# %%

import torch as t

# trying to import [AutoModelForMaskedLM] from the non-private location fucks up, not sure why; it makes
# [from_pretrained == None]
from transformers import AutoModelForMaskedLM
from transformers.modeling_outputs import MaskedLMOutput
from transformers.models.auto.tokenization_auto import AutoTokenizer

from easy_transformer import EasyBERT


def test_bert():
    model_name = "bert-base-uncased"
    text = "Hello world!"
    model = EasyBERT.EasyBERT.from_pretrained(model_name)  # TODO why two?
    output: MaskedLMOutput = model(text)  # TODO need to change the type

    assert output.logits.shape == (1, 5, model.config.d_vocab)  # TODO why 5?

    # now let's compare it to the HuggingFace version
    assert (
        AutoModelForMaskedLM.from_pretrained is not None
    )  # recommended by https://github.com/microsoft/pylance-release/issues/333#issuecomment-688522371
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForMaskedLM.from_pretrained(model_name)
    hf_output = model(**tokenizer(text, return_tensors="pt"))
    assert t.allclose(output.logits, hf_output.logits, atol=1e-4)


def test_embeddings():
    hf = AutoModelForMaskedLM.from_pretrained("bert-base-uncased")
    model = EasyBERT.EasyBERT.from_pretrained("bert-base-uncased")
    assert t.allclose(
        hf.bert.embeddings.word_embeddings.weight,
        model.embeddings.word_embeddings.weight,
        atol=1e-4,
    )


# %%


# TODO make an anki about this workflow- including function scope for name conflicts

# now we test output
# TODO ensure that our model matches the architecture diagram of BERT


def make_this_a_test():
    model_name = "bert-base-uncased"
    text = "Hello world!"
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    our_model = EasyBERT.EasyBERT.from_pretrained(model_name)  # TODO why two?
    output: MaskedLMOutput = our_model(text)  # TODO need to change the type

    n_tokens_in_input = tokenizer(text, return_tensors="pt")
    assert n_tokens_in_input == 5
    assert output.logits.shape == (1, n_tokens_in_input, our_model.config.d_vocab)

    assert (
        AutoModelForMaskedLM.from_pretrained is not None
    )  # recommended by https://github.com/microsoft/pylance-release/issues/333#issuecomment-688522371
    hugging_face_model = AutoModelForMaskedLM.from_pretrained(model_name)
    hf_output = hugging_face_model(**tokenizer(text, return_tensors="pt"))
    assert t.allclose(output.logits, hf_output.logits, atol=1e-4)


# %%


def test_that_im_awesome():
    model_name = "bert-base-uncased"
    text = "Hello world!"
    from transformers.models.auto.tokenization_auto import AutoTokenizer

    from easy_transformer import EasyBERT

    atol = 1e-1

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    our_model = EasyBERT.EasyBERT.from_pretrained(model_name)
    # TODO add various [return_type] options
    # TODO figure out what's up with [using eos_token] and [using bos_token]
    our_output: EasyBERT.Output = our_model(
        text
    )  # TODO set the type of the variable on the LHS

    # trying to import [AutoModelForMaskedLM] from the non-private location fucks up, not sure why; it makes
    # [from_pretrained == None]
    from transformers import AutoModelForMaskedLM

    n_tokens_in_input = tokenizer(text, return_tensors="pt")["input_ids"].shape[1]
    assert n_tokens_in_input == 5
    assert our_output.logits.shape == (1, n_tokens_in_input, our_model.config.d_vocab)
    assert our_output.embedding.shape == (
        1,
        n_tokens_in_input,
        our_model.config.hidden_size,
    )
    assert our_output.hidden_states.shape == (
        our_model.config.n_layers,
        1,
        n_tokens_in_input,
        our_model.config.hidden_size,
    )

    assert (
        AutoModelForMaskedLM.from_pretrained is not None
    )  # recommended by https://github.com/microsoft/pylance-release/issues/333#issuecomment-688522371
    hugging_face_model = AutoModelForMaskedLM.from_pretrained(model_name)
    hf_output: MaskedLMOutput = hugging_face_model(
        **tokenizer(text, return_tensors="pt"),
        output_hidden_states=True,
    )

    # let's check the embeddings

    assert t.allclose(
        our_output.embedding, hf_output.hidden_states[0], atol=atol
    )  # TODO higher precision (lower atol)?  i think it's because of a limitation in the size of the floats? not sure! otherwise i got floating point rouding errors, i think

    assert our_output.logits.shape == hf_output.logits.shape
    assert t.allclose(our_output.logits, hf_output.logits, atol=atol)