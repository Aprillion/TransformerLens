import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

import torch as t
import torch.nn as nn
import torch.nn.functional as F
from torchtyping import TensorType as TT
from transformers import (
    AutoModelForMaskedLM,  # unfortunately the suggestion to import from the non-private location doesn't work; it makes [from_pretrained == None]
)
from transformers import (
    PreTrainedTokenizer,  # TODO why is this split up- move the comment around?
)
from transformers.models.auto.tokenization_auto import AutoTokenizer

from easy_transformer.hook_points import HookedRootModule

from .. import loading_from_pretrained as loading
from . import attention, embeddings, encoder, encoder_layer
from .config import Config

# TODO share this type declaration with [EasyTransformer.py]
TokensTensor = TT["batch", "pos"]
InputForForwardLayer = Union[str, List[str], TokensTensor]


@dataclass
class Output:
    logits: TT["batch", "seq", "vocab"]
    hidden_states: TT[
        "n_layers", "batch", "seq", "hidden"
    ]  # slightly different than HF which stacks embeddings and hidden states
    embedding: TT["batch", "seq", "hidden"]
    attentions_post_softmax: TT[
        "n_layers", "batch", "head", "seq", "seq"
    ]  # similar to HF:  https://huggingface.co/docs/transformers/model_doc/bert#transformers.BertForMaskedLM.forward.output_attentions


class EasyBERT(HookedRootModule):
    # written in this style because this guarantees that [self] isn't set inside here
    @classmethod
    def from_pretrained(
        cls,
        model_name: str,
        **model_kwargs,
    ):
        logging.info(f"Loading model: {model_name}")
        official_model_name = loading.get_official_model_name(model_name)
        # TODO the fact that pylance / w.e. doesn't show the members of the literal is a little gros :(
        hidden_size = 768
        config = Config(
            layers=12,
            heads=12,
            hidden_size=hidden_size,
            head_size=hidden_size,
            dropout=0.0,  # TODO change
            model=official_model_name,
            vocab_size=30522,
            max_length=512,
            tokenizer=official_model_name,
            mlp_size=4 * hidden_size,
        )  # TODO fancier :P
        assert AutoModelForMaskedLM.from_pretrained is not None
        state_dict = AutoModelForMaskedLM.from_pretrained(
            official_model_name
        ).state_dict()
        model = cls(config, **model_kwargs)
        model.load_and_process_state_dict(state_dict)
        logging.info(
            f"Finished loading pretrained model {model_name} into EasyTransformer!"
        )

        return model

    @classmethod
    def __generate_tokenizer__(
        cls, config: Config, tokenizer: Optional[PreTrainedTokenizer]
    ):
        if tokenizer is not None:
            return tokenizer

        if config.tokenizer is not None:
            # If we have a tokenizer name, we can load it from HuggingFace
            result: PreTrainedTokenizer = AutoTokenizer.from_pretrained(
                config.tokenizer
            )
            result.eos_token = (
                result.eos_token if result.eos_token is not None else "<|endoftext|>"
            )
            result.pad_token = (
                result.pad_token if result.pad_token is not None else result.eos_token
            )
            result.bos_token = (
                result.bos_token if result.bos_token is not None else result.eos_token
            )
            return result
        else:
            # If no tokenizer name is provided, we assume we're training on an algorithmic task and will pass in tokens directly. In this case, we don't need a tokenizer.
            return None

    def __init__(self, config: Config, tokenizer=None, **kwargs):
        # TODO what are the kwargs used for?
        super().__init__()
        self.config = config
        self.tokenizer = EasyBERT.__generate_tokenizer__(self.config, tokenizer)
        self.embeddings = embeddings.Embeddings(config)
        self.encoder = encoder.Encoder(config)
        self.out_linear = nn.Linear(config.hidden_size, config.hidden_size)
        self.out_ln = nn.LayerNorm(
            config.hidden_size, eps=1e-12, elementwise_affine=True
        )
        self.unembed = nn.parameter.Parameter(t.zeros(config.vocab_size))

    # TODO utils?
    def __copy__(self, mine, state_dict, base_name):
        mine.weight.detach().copy_(state_dict[base_name + ".weight"])
        if base_name + ".bias" in state_dict:
            mine.bias.detach().copy_(state_dict[base_name + ".bias"])

    def __load_embedding_state_dict__(self, state_dict: Dict[str, t.Tensor]):
        _copy_ = lambda mine, base_name: self.__copy__(mine, state_dict, base_name)
        _copy_(
            self.embeddings.word_embeddings,
            "bert.embeddings.word_embeddings",
        )
        _copy_(
            self.embeddings.position_embeddings, "bert.embeddings.position_embeddings"
        )
        _copy_(
            self.embeddings.token_type_embeddings,
            "bert.embeddings.token_type_embeddings",
        )
        _copy_(self.embeddings.ln, "bert.embeddings.LayerNorm")

    def __load_cls_state_dict__(self, state_dict: Dict[str, t.Tensor]):
        """
        'cls.predictions.bias', 'cls.predictions.transform.dense.weight', 'cls.predictions.transform.dense.bias', 'cls.predictions.transform.LayerNorm.weight', 'cls.predictions.transform.LayerNorm.bias', ])"""
        _copy_ = lambda mine, base_name: self.__copy__(mine, state_dict, base_name)
        _copy_(self.out_linear, "cls.predictions.transform.dense")
        _copy_(self.out_ln, "cls.predictions.transform.LayerNorm")

    def __load_layer_state_dict__(
        self,
        layer_index: int,
        state_dict: Dict[str, t.Tensor],
    ):
        base_name = f"bert.encoder.layer.{layer_index}"
        _copy_ = lambda mine, name: self.__copy__(
            mine, state_dict, base_name + "." + name
        )

        # copy attention stuff
        attention_output = self.encoder.layers[layer_index].attention
        assert isinstance(attention_output, attention.Attention)
        self_attention = attention_output.self_attention
        _copy_(mine=self_attention.w_q, name="attention.self.query")
        _copy_(mine=self_attention.w_k, name="attention.self.key")
        _copy_(mine=self_attention.w_v, name="attention.self.value")
        _copy_(mine=self_attention.w_o, name="attention.output.dense")

        # copy intermediate layer norm
        _copy_(mine=attention_output.ln, name="attention.output.LayerNorm")

        # copy mlp stuff
        mlp = self.encoder.layers[layer_index].mlp
        assert isinstance(mlp, encoder_layer.MLP)
        _copy_(mine=mlp.w_1, name="intermediate.dense")
        _copy_(mine=mlp.w_2, name="output.dense")
        _copy_(mine=mlp.ln, name="output.LayerNorm")

    def __load_encoder_state_dict__(self, state_dict):
        for layer_index in range(self.config.layers):
            self.__load_layer_state_dict__(layer_index, state_dict=state_dict)

    def load_and_process_state_dict(self, state_dict: Dict[str, t.Tensor]):
        self.__load_embedding_state_dict__(state_dict)
        self.__load_encoder_state_dict__(state_dict)
        # TODO probably rename this
        self.__load_cls_state_dict__(state_dict)
        self.unembed.detach().copy_(state_dict["cls.predictions.bias"])

        fail = False
        for name, p in self.named_parameters():
            if t.isnan(p).any():
                print(f"Forgot to initialize: {name}")
                fail = True
            else:
                p.requires_grad_(True)
        assert not fail

    # TODO duplicated code, share it
    def __make_tokens_for_forward__(
        self, x: InputForForwardLayer, prepend_bos: bool
    ) -> TokensTensor:
        tokens: t.Tensor  # set inside the function body
        if type(x) == str or type(x) == list:
            # If text, convert to tokens (batch_size=1)
            assert (
                self.tokenizer is not None
            ), "Must provide a tokenizer if passing a string to the model"
            # This is only intended to support passing in a single string
            # TODO why does this have type errors ! ! ! ! !: (?
            # TODO can we solve it in the other place too?
            tokens = self.to_tokens(x, prepend_bos=prepend_bos)
        else:
            assert isinstance(
                x, t.Tensor
            )  # typecast; we know that this is ok because of the above logic
            tokens = x
        if len(tokens.shape) == 1:
            # If tokens are a rank 1 tensor, add a dummy batch dimension to avoid things breaking.
            tokens = tokens[None]
        if tokens.device.type != self.config.device:
            tokens = tokens.to(self.config.device)
        assert isinstance(tokens, t.Tensor)
        return tokens

    def __to_segment_ids__(self, tokens: TokensTensor) -> t.Tensor:
        # TODO this is a bit hacky, but it works. We should probably make a proper segment id tensor
        # lol thanks copilot, which suggested zeros_like
        assert self.tokenizer is not None
        tokenizer: PreTrainedTokenizer = self.tokenizer  # for pylance
        result = tokenizer(tokens, return_tensors="pt", padding=True)["token_type_ids"]
        assert isinstance(result, t.Tensor)  # TODO is this ok? done for pylance
        return result

    def __make_segment_ids__(
        self, x: InputForForwardLayer, passed_segment_ids: Optional[TT["batch", "seq"]]
    ) -> TT["batch", "seq"]:
        # TODO is this right? :) copilot did it
        result: TT["batch", "seq"] = None
        if passed_segment_ids is None:
            if type(x) == str or type(x) == list:
                # If text, convert to tokens (batch_size=1)
                assert (
                    self.tokenizer is not None
                ), "Must provide a tokenizer if passing a string to the model"
                # This is only intended to support passing in a single string
                result = self.__to_segment_ids__(x)
            else:
                assert isinstance(x, t.Tensor)
        else:
            result = passed_segment_ids
        return result

    # TODO add [return_type] and maybe [prepend_bos] and maybe [past_kv_cache] and maybe [append_eos]
    def forward(
        self, x: InputForForwardLayer, segment_ids: TT["batch", "seq"] = None
    ) -> Output:
        # TODO document [segment_ids]
        # attention masking for padded token
        # t.ByteTensor([batch_size, 1, seq_len, seq_len)
        tokens = self.__make_tokens_for_forward__(
            x, prepend_bos=False
        )  # TODO really, always False?
        actual_segment_ids: TT["batch", "seq"] = self.__make_segment_ids__(
            x=x, passed_segment_ids=segment_ids
        )  # TODO prepend_bos=False?
        """
        mask = (
            (tokens > 0).unsqueeze(1).repeat(1, tokens.size(1), 1).unsqueeze(1)
        )  # TODO is this right..?
        """
        mask = None  # TODO put mask back in
        embedded = self.embeddings(
            tokens,
            actual_segment_ids,
        )  # TODO is there a way to make python complain about the variable named [input]?
        encoder_output: encoder.Output = self.encoder(embedded, mask=mask)
        hidden_states = encoder_output.hidden_states
        attentions_post_softmax = encoder_output.attentions_post_softmax
        last_hidden_state = hidden_states[-1]
        # TODO return both NSP and MLM logits
        output = self.out_linear(last_hidden_state)
        output = F.gelu(output)
        output = self.out_ln(output)
        output = t.einsum("vh,bsh->bsv", self.embeddings.word_embeddings.weight, output)
        logits = output + self.unembed
        return Output(
            logits=logits,
            embedding=embedded,
            hidden_states=hidden_states,
            attentions_post_softmax=attentions_post_softmax,
        )

    # TODO maybe change the order?
    def to_tokens(
        self,
        x: Union[str, List[str]],
        prepend_bos: bool = True,
        move_to_device: bool = True,
    ) -> TT["batch", "pos"]:  # TODO change this type
        """
        Converts a string to a tensor of tokens. If prepend_bos is True, prepends the BOS token to the input - this is recommended when creating a sequence of tokens to be input to a model.

        Gotcha: prepend_bos prepends a beginning of string token. This is a recommended default when inputting a prompt to the model as the first token is often treated weirdly, but should only be done at the START of the prompt. Make sure to turn it off if you're looking at the tokenization of part of the prompt!
        (Note: some models eg GPT-2 were not trained with a BOS token, others (OPT and my models) were)

        Gotcha2: Tokenization of a string depends on whether there is a preceding space and whether the first letter is capitalized. It's easy to shoot yourself in the foot here if you're not careful!
        """
        assert self.tokenizer is not None, "Cannot use to_tokens without a tokenizer"
        if prepend_bos:
            if isinstance(x, str):
                x = self.tokenizer.bos_token + x
            else:
                x = [self.tokenizer.bos_token + string for string in x]
        tokens = self.tokenizer(x, return_tensors="pt", padding=True)["input_ids"]
        if move_to_device:
            # TODO why did pylance not complain about [self.cfg]
            assert isinstance(tokens, t.Tensor)
            tokens = tokens.to(self.config.device)
        return tokens