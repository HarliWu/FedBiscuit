import sys
import logging
import torch
import transformers
from transformers import pipeline, GenerationConfig
import os
import gc

transformers.logging.set_verbosity(40)

from federatedscope.core.configs.config import global_cfg
from federatedscope.core.cmd_args import parse_args, parse_client_cfg
from federatedscope.llm.dataloader.dataloader import get_tokenizer
from federatedscope.llm.model.model_builder import get_llm
from federatedscope.llm.dataset.llm_dataset import PROMPT_DICT
from federatedscope.core.auxiliaries.utils import setup_seed
from federatedscope.core.auxiliaries.logging import update_logger
from federatedscope.llm.offsite_tuning.utils import \
    wrap_offsite_tuning_for_eval

logger = logging.getLogger(__name__)


class FSChatBot(object):
    def __init__(self, config, use_raw=False):
        self.config = config

        self.device = f'cuda:{config.device}'
        self.add_special_tokens = True

        num_ckpt = config.federate.total_round_num // config.federate.save_freq
        self.prefix = ['final_'] + \
                      [str(i*config.federate.save_freq) + '_'
                       for i in range(num_ckpt, -1, -1)] + ['']
        self.dirname, self.filename = os.path.split(config.federate.save_to)
        print(self.prefix)
        if use_raw:
            self.use_raw_model()
        else:
            self.next_model()

    def use_raw_model(self):
        if hasattr(self, 'model'):
            delattr(self, 'model')
            gc.collect()
            torch.cuda.empty_cache()

        model_name, _ = self.config.model.type.split('@')
        self.tokenizer, _ = get_tokenizer(model_name, self.config.data.root,
                                          self.config.llm.tok_len)
        
        self.model = get_llm(self.config,
                             device_map='auto')

        logger.info("will use raw model.")
        print("will use raw model.")

        self.model = self.model.to(self.device + 1)
        self.model = self.model.eval()
        if torch.__version__ >= "2" and sys.platform != "win32":
            self.model = torch.compile(self.model)

        self.max_history_len = self.config.llm.chat.max_history_len
        self.max_len = self.config.llm.chat.max_len
        self.history = []

    def next_model(self):
        if hasattr(self, 'model'):
            delattr(self, 'model')
            gc.collect()

        model_name, _ = self.config.model.type.split('@')
        self.tokenizer, _ = get_tokenizer(model_name, self.config.data.root,
                                          self.config.llm.tok_len)

        self.model = get_llm(self.config,
                             device_map='auto')
        self.generation_config = GenerationConfig.from_pretrained(model_name)
        logger.info(f'{model_name} default generation setting: '
                    f'{self.generation_config}')

        self.curpfx = None
        for pre in self.prefix:
            if os.path.exists(os.path.join(self.dirname, pre + self.filename)):
                self.curpfx = pre
                break

        # Load model from the checkpoints
        if self.curpfx is not None:
            ckpt_path = os.path.join(self.dirname, self.curpfx + self.filename)
            if self.config.llm.offsite_tuning.use:
                self.model = wrap_offsite_tuning_for_eval(
                    self.model, self.config, ckpt_path)
            else:
                ckpt = torch.load(ckpt_path, map_location='cpu')
                if 'model' and 'cur_round' in ckpt:
                    self.model.load_state_dict(ckpt['model'])
                    logger.info(
                        f"Load with the model of Round {ckpt['cur_round']}")
                    print(f"Load with the model of Round {ckpt['cur_round']}")
                else:
                    self.model.load_state_dict(ckpt)
            logger.info(f'Model loads from the checkpoint {ckpt_path}')
            print(f'Model loads from the checkpoint {ckpt_path}')

            # remove the prefix up to the current one
            self.prefix = self.prefix[self.prefix.index(self.curpfx) + 1:]

        elif len(self.prefix) > 1:
            logger.info("will use raw model.")
            print("will use raw model.")
            self.prefix = []
            if self.config.llm.offsite_tuning.use:
                self.model = wrap_offsite_tuning_for_eval(
                    self.model, self.config)
        else:
            raise ValueError('No more model is able to us')

        self.model = self.model.eval()
        if torch.__version__ >= "2" and sys.platform != "win32":
            self.model = torch.compile(self.model)

        # Create the generation pipeline
        self.generation_pipe = pipeline('text-generation',
                                        model=self.model,
                                        tokenizer=self.tokenizer,
                                        device_map='auto',
                                        trust_remote_code='True')

        self.max_history_len = self.config.llm.chat.max_history_len
        self.max_len = self.config.llm.chat.max_len
        self.history = []

    def _build_prompt(self, input_text):
        source = {'instruction': input_text}
        return PROMPT_DICT['prompt_no_input'].format_map(source)

    def predict(self, input_text, use_history=True, use_prompt=True):
        if use_prompt:
            input_text = self._build_prompt(input_text)
        text_ids = self.tokenizer.encode(input_text, add_special_tokens=False)
        self.history.append(text_ids)
        input_ids = []
        if use_history:
            for history_ctx in self.history[-self.max_history_len:]:
                input_ids.extend(history_ctx)
        else:
            input_ids.extend(text_ids)
        input_ids = torch.tensor(input_ids).long()
        input_ids = input_ids.unsqueeze(0).to(self.device)
        response = self.model.generate(input_ids=input_ids,
                                       max_new_tokens=self.max_len,
                                       num_beams=4,
                                       no_repeat_ngram_size=2,
                                       early_stopping=True,
                                       temperature=0.2)

        self.history.append(response[0].tolist())
        response_tokens = \
            self.tokenizer.decode(response[0][input_ids.shape[1]:],
                                  skip_special_tokens=True)
        return response_tokens

    @torch.no_grad()
    def generate(self, input_text, generate_kwargs={}):
        if type(input_text) is str:
            input_text_tokens = self.tokenizer(
                input_text,
                padding=False,
                add_special_tokens=True,
                return_tensors="pt",
            )
            input_ids = input_text_tokens.input_ids.to('cuda')
            attention_mask = input_text_tokens.attention_mask.to('cuda')
            # generate_kwargs['generation_config'] = self.generation_config

            output_ids = self.model.generate(input_ids=input_ids,
                                             attention_mask=attention_mask,
                                             **generate_kwargs)

            response = []
            for i in range(output_ids.shape[0]):
                response.append(
                    self.tokenizer.decode(output_ids[i][input_ids.shape[1]:],
                                          skip_special_tokens=True,
                                          ignore_tokenization_space=True))

            if len(response) > 1:
                return response
            return response[0]

        else:
            response = self.generation_pipe(input_text,
                                            return_full_text=False,
                                            **generate_kwargs)

            if len(response) > 1:
                return [ans['generated_text'] for ans in response]
            return response[0]['generated_text']

    def clear(self):
        self.history = []


def main():
    init_cfg = global_cfg.clone()
    args = parse_args()
    if args.cfg_file:
        init_cfg.merge_from_file(args.cfg_file)
    cfg_opt, client_cfg_opt = parse_client_cfg(args.opts)
    init_cfg.merge_from_list(cfg_opt)

    update_logger(init_cfg, clear_before_add=True)
    setup_seed(init_cfg.seed)

    chat_bot = FSChatBot(init_cfg)
    welcome = "Welcome to FSChatBot, " \
              "`clear` to clear history, " \
              "`quit` to end chat."
    print(welcome)
    while True:
        input_text = input("\nUser:")
        if input_text.strip() == "quit":
            break
        if input_text.strip() == "clear":
            chat_bot.clear()
            print(welcome)
            continue
        print(f'\nFSBot: {chat_bot.predict(input_text)}')


if __name__ == "__main__":
    main()
