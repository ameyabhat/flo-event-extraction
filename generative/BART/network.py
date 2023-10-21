import torch 
from torch import nn 
from torch.nn import functional as F 
from transformers import (
    AdamW,
    get_linear_schedule_with_warmup,
    BartModel,
    
)
from transformers.modeling_utils import PreTrainedModel
from transformers.modeling_outputs import Seq2SeqLMOutput



class BartGen(PreTrainedModel):
    def __init__(self, config, tokenizer):
        super(BartGen, self).__init__(config)
        self.config = config 
        self.tokenizer = tokenizer 
        self.transformer = BartModel.from_pretrained('facebook/bart-large')
        self.register_buffer("final_logits_bias", torch.zeros((1, self.transformer.shared.num_embeddings)))

    def resize_token_embeddings(self):
        old_num_tokens = self.transformer.shared.num_embeddings
        new_embeddings = self.transformer.resize_token_embeddings(len(self.tokenizer))
        self.transformer.shared = new_embeddings
        self._resize_final_logits_bias(len(self.tokenizer), old_num_tokens)
        self.vocab_size = len(self.tokenizer) 

        return new_embeddings

    def _resize_final_logits_bias(self, new_num_tokens: int, old_num_tokens: int) -> None:
        if new_num_tokens <= old_num_tokens:
            new_bias = self.final_logits_bias[:, :new_num_tokens]
        else:
            extra_bias = torch.zeros((1, new_num_tokens - old_num_tokens), device=self.final_logits_bias.device)
            new_bias = torch.cat([self.final_logits_bias, extra_bias], dim=1)
        self.register_buffer("final_logits_bias", new_bias)


    def _init_weights(self, module):
        """ Initialize the weights """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif isinstance(module, torch.nn.LayerNorm): # if use apex, this should be FusedLayerNorm 
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()


    def get_encoder(self):
        return self.transformer.encoder 

        
    def get_output_embeddings(self):
        # this method is needed for generation
        vocab_size, emb_size = self.transformer.shared.weight.shape
        lin_layer = nn.Linear(vocab_size, emb_size, bias=False)
        lin_layer.weight.data = self.transformer.shared.weight.data
        return lin_layer 


    def prepare_inputs_for_generation(
        self, decoder_input_ids, past, attention_mask, use_cache, encoder_outputs, **kwargs
    ):
        return {
            "input_ids": None,  # encoder_outputs is defined. input_ids not needed
            "encoder_outputs": encoder_outputs,
            "past_key_values": past,
            "decoder_input_ids": decoder_input_ids,
            "attention_mask": attention_mask,
            "use_cache": use_cache,  # change this to avoid caching (presumably for debugging)
        }

    def adjust_logits_during_generation(self, logits, cur_len, max_length):
        if cur_len == 1: #and self.config.force_bos_token_to_be_generated:
            self._force_token_ids_generation(logits, self.config.bos_token_id)
        elif cur_len == max_length - 1 and self.config.eos_token_id is not None:
            self._force_token_ids_generation(logits, self.config.eos_token_id)
        return logits

    def _force_token_ids_generation(self, scores, token_id) -> None:
        """force one of token_ids to be generated by setting prob of all other tokens to 0 (logprob=-float("inf"))"""
        scores[:, [x for x in range(self.config.vocab_size) if x != token_id]] = -float("inf")

    @staticmethod
    def _reorder_cache(past, beam_idx):
        # reordered_past = []
        # print(past, len(past))
        # for layer_past in past:
        #     # get the correct batch idx from decoder layer's batch dim for cross and self-attn
        #     layer_past_new = {
        #         attn_key: _reorder_buffer(attn_cache, beam_idx) for attn_key, attn_cache in layer_past.items()
        #     }
        #     reordered_past.append(layer_past_new)
        return past
    
    

    def forward(self, input_ids, 
        attention_mask=None, 
        encoder_outputs=None, 
        use_cache=False,
        past_key_values=None,
        decoder_input_ids=None, 
        decoder_attention_mask=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None, 
        task=-1):

        # generation
        if task==-1:
            outputs = self.transformer(
            input_ids=input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            use_cache=use_cache, 
            encoder_outputs=encoder_outputs,
            past_key_values=past_key_values,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,)

            lm_logits = F.linear(outputs[0], self.transformer.shared.weight, bias=self.final_logits_bias)
            masked_lm_loss = None
            
            if not return_dict:
                output = (lm_logits,) + outputs[1:]
                return ((masked_lm_loss,) + output) if masked_lm_loss is not None else output

            return Seq2SeqLMOutput(
                loss=masked_lm_loss,
                logits=lm_logits,
                past_key_values=outputs.past_key_values,
                decoder_hidden_states=outputs.decoder_hidden_states,
                decoder_attentions=outputs.decoder_attentions,
                encoder_last_hidden_state=outputs.encoder_last_hidden_state,
                encoder_hidden_states=outputs.encoder_hidden_states,
                encoder_attentions=outputs.encoder_attentions,
            )
            
        #training 
        elif task==0:
            
            assert(decoder_input_ids!=None)
            y_ids = decoder_input_ids[:, :-1] 
            labels = decoder_input_ids[:, 1:].clone() 
            labels[labels== self.tokenizer.pad_token_id] = -100 
            # labels are just decoder_input_ids shifted to the right by 1 
            
            outputs = self.transformer(
            input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=y_ids,
            decoder_attention_mask=decoder_attention_mask[:, :-1],
            use_cache=False, 
            past_key_values=past_key_values,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,)
            
            sequence_output = outputs[0]
            
            lm_logits = F.linear(sequence_output, self.transformer.shared.weight, bias=self.final_logits_bias)
            outputs = (lm_logits,) + outputs[1:]  # Add cache, hidden states and attention if they are here
            loss_fct = nn.CrossEntropyLoss()

            masked_lm_loss = loss_fct(lm_logits.view(-1, self.vocab_size), labels.view(-1))
            outputs = (masked_lm_loss,) + outputs

            return outputs

    
    
        