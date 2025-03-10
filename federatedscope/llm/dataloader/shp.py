import os
import json
import copy
import pickle
import datasets

from federatedscope.core.splitters.generic.lda_splitter import LDASplitter
from federatedscope.core.data.utils import download_url
from federatedscope.llm.dataloader.dataloader import load_jsonls, load_jsonl
from federatedscope.llm.dataset.llm_dataset import LLMComparisonDataset, \
    LLMDataset

SHP_PROMPT_DICT = {
    "shp": ("Below is an instruction that describes a task. "
            "Write a response that appropriately completes the request.\n\n"
            "### Instruction:\n{instruction}\n\n"
            "### Response:"),
    "shp_cmp": ("Below is a query followed by two responses. Pick a "
                "helpful response that is precise, concise, and casual. "
                "State your choice with a single capital letter, "
                "i.e., \"A\" if RESPONSE A is better, "
                "\"B\" if RESPONSE B is better.\n\n"
                "### QUERY: {instruction}\n"
                "### RESPONSE A: {output_A}\n"
                "### RESPONSE B: {output_B}\n"
                "### YOUR CHOICE:"),
    "mix_cmp": ("Below is an instruction that describes a task. "
                "There are two responses that complete the request. "
                "Pick an appropriate response and state your choice with "
                "a single capital letter, i.e., "
                "\"A\" if RESPONSE A is better and more appropriate, "
                "\"B\" if RESPONSE B is better and more appropriate.\n\n"
                "### Instruction:\n{instruction}\n\n"
                "### RESPONSE A: {output_A}\n"
                "### RESPONSE B: {output_B}\n"
                "### YOUR CHOICE:")
}


def _download_shp_cmpr(data_root):
    train_fp, val_fp, test_fp = [
        os.path.join(data_root, 'shp_cmpr_train.jsonl'),
        os.path.join(data_root, 'shp_cmpr_val.jsonl'),
        os.path.join(data_root, 'shp_cmpr_test.jsonl')
    ]

    dataloader_kwargs = {
        'instruction': 'instruction',
        'output_A': 'output_A',
        'output_B': 'output_B',
        'choice': 'choice',
        'category': 'category'
    }
    if os.path.exists(train_fp) and os.path.exists(val_fp) and \
            os.path.exists(test_fp):
        list_train_dict = load_jsonl(train_fp, **dataloader_kwargs)
        list_val_dict = load_jsonl(val_fp, **dataloader_kwargs)
        list_test_dict = load_jsonl(test_fp, **dataloader_kwargs)

    else:
        dataset = datasets.load_dataset("stanfordnlp/SHP")
        list_train_dict, list_val_dict, list_test_dict = [], [], []
        tag_fp = {
            'train': (train_fp, list_train_dict),
            'validation': (val_fp, list_val_dict),
            'test': (test_fp, list_test_dict)
        }
        for tag, (fp, list_data_dict) in tag_fp.items():
            file = open(fp, 'w')
            for hist, ref_A, ref_B, choice, domain in \
                zip(dataset[tag]['history'],
                    dataset[tag]['human_ref_A'],
                    dataset[tag]['human_ref_B'],
                    dataset[tag]['labels'],
                    dataset[tag]['domain']):
                record = {
                    'instruction': hist,
                    'output_A': ref_A,
                    'output_B': ref_B,
                    'choice': choice,
                    'category': domain.split('_')[0]
                }
                file.write(f'{json.dumps(record)}\n')
                list_data_dict.append(record)
            file.close()

    return list_train_dict, list_val_dict, list_test_dict


def _download_shp(data_root):
    train_fp, val_fp, test_fp = [
        os.path.join(data_root, 'shp_rlhf_train.jsonl'),
        os.path.join(data_root, 'shp_rlhf_val.jsonl'),
        os.path.join(data_root, 'shp_rlhf_test.jsonl')
    ]

    dataloader_kwargs = {'instruction': 'instruction', 'category': 'category'}
    if os.path.exists(train_fp) and os.path.exists(val_fp) and \
            os.path.exists(test_fp):
        list_train_dict = load_jsonl(train_fp, **dataloader_kwargs)
        list_val_dict = load_jsonl(val_fp, **dataloader_kwargs)
        list_test_dict = load_jsonl(test_fp, **dataloader_kwargs)

    else:
        dataset = datasets.load_dataset("stanfordnlp/SHP")
        instructions = []
        list_train_dict, list_val_dict, list_test_dict = [], [], []
        tag_fp = {
            'train': (train_fp, list_train_dict),
            'validation': (val_fp, list_val_dict),
            'test': (test_fp, list_test_dict)
        }
        for tag, (fp, list_data_dict) in tag_fp.items():
            file = open(fp, 'w')
            for hist, domain in zip(dataset[tag]['history'],
                                    dataset[tag]['domain']):
                if hist not in instructions:
                    instructions.append(hist)
                    record = {
                        'instruction': hist,
                        'category': domain.split('_')[0]
                    }
                    file.write(f'{json.dumps(record)}\n')
                    list_data_dict.append(record)
            file.close()

    return list_train_dict, list_val_dict, list_test_dict


def shp_dataset(data_root, num_clients, tokenizer):
    list_train_dict, list_val_dict, list_test_dict = \
        _download_shp_cmpr(data_root)

    # First, disjoint by post instructions
    list_train_instructions, _, _ = _download_shp(data_root)
    cat_idx_map = {}
    for sample in list_train_instructions:
        if sample['category'] not in cat_idx_map:
            cat_idx_map[sample['category']] = len(cat_idx_map)
        sample['categories'] = cat_idx_map[sample['category']]

    # Second, use Dirichlet data splitter
    splitter = LDASplitter(num_clients, alpha=0.3)
    inst_split_list = splitter(list_train_instructions)
    inst_client_map = {}
    for idx, sublist in enumerate(inst_split_list):
        for sample in sublist:
            inst_client_map[sample['instruction']] = idx

    # Update their categories and force the data splitter as meta
    for sample in list_train_dict:
        sample['domain'] = sample['category']
        sample['category'] = \
            f"Client_{inst_client_map[sample['instruction']]}"

    # Select the data with less or equal to 512 tokens
    new_list_train_dict = []
    for sample in list_train_dict:
        len_inst = len(tokenizer(sample['instruction'])['input_ids'])
        len_resA = len(tokenizer(sample['output_A'])['input_ids'])
        len_resB = len(tokenizer(sample['output_B'])['input_ids'])
        if len_inst + len_resA + len_resB <= 512:
            new_list_train_dict.append(sample)
    list_train_dict = new_list_train_dict

    # Print the samples of each domain for each clients
    for client_id in range(num_clients + 1):
        print(f'Client {client_id}:')
        num_sample_by_domains = dict()
        for sample in new_list_train_dict:
            if sample['category'] == f'Client_{client_id}':
                if sample['domain'] not in num_sample_by_domains:
                    num_sample_by_domains[sample['domain']] = 0
                num_sample_by_domains[sample['domain']] += 1
        print(num_sample_by_domains)

    return list_train_dict, list_val_dict, list_test_dict


def load_rlhf_dataset(data_root,
                      tokenizer,
                      max_num_test=-1,
                      raw_no_prompt=False):
    _, list_val_dict, list_test_dict = \
        _download_shp(data_root)

    # reorganize the training data for RLHF
    list_train_dict = list_val_dict + list_test_dict
    list_val_dict = list_test_dict[:len(list_test_dict) // 2]
    list_test_dict = list_test_dict[len(list_test_dict) // 2:]

    if max_num_test > 0:
        return (list_train_dict, list_val_dict[:max_num_test],
                list_test_dict[:max_num_test])
    else:
        return list_train_dict, list_val_dict, list_test_dict


def load_safe_dataset():
    ds = datasets.load_dataset("PKU-Alignment/PKU-SafeRLHF-prompt")
    list_train_dict = [{
        'instruction': prompt
    } for prompt in ds['train']['prompt']]

    return list_train_dict, None, None


def load_comparison_dataset(data_root, tokenizer, config, max_num_test=-1):
    token_name = os.path.basename(tokenizer.name_or_path)
    num_clients = config.federate.client_num
    train_fp, val_fp, test_fp = [
        os.path.join(data_root, f'{token_name}_train_{num_clients}.pickle'),
        os.path.join(data_root, f'{token_name}_val.pickle'),
        os.path.join(data_root, f'{token_name}_test.pickle')
    ]

    if os.path.exists(train_fp) and os.path.exists(val_fp) and os.path.exists(
            test_fp):
        with open(train_fp, 'rb') as f_train, open(val_fp, 'rb') as f_val, \
                open(test_fp, 'rb') as f_test:
            train_dataset = pickle.load(f_train)
            val_dataset = pickle.load(f_val)
            test_dataset = pickle.load(f_test)

    else:
        list_train_dict, list_val_dict, list_test_dict = \
            shp_dataset(data_root, num_clients, tokenizer)

        # load dataset, which should be tuple
        train_dataset = LLMComparisonDataset(
            list_train_dict,
            tokenizer,
            prompt_input=SHP_PROMPT_DICT['shp'],
            prompt_no_input=SHP_PROMPT_DICT['shp'],
            output_A='output_A',
            output_B='output_B',
            choice='choice')
        val_dataset = LLMComparisonDataset(
            list_val_dict,
            tokenizer,
            prompt_input=SHP_PROMPT_DICT['shp'],
            prompt_no_input=SHP_PROMPT_DICT['shp'],
            output_A='output_A',
            output_B='output_B',
            choice='choice')
        test_dataset = LLMComparisonDataset(
            list_test_dict,
            tokenizer,
            prompt_input=SHP_PROMPT_DICT['shp'],
            prompt_no_input=SHP_PROMPT_DICT['shp'],
            output_A='output_A',
            output_B='output_B',
            choice='choice')

        # Store these three lists to a pickle file
        with open(train_fp, 'wb') as f_train, \
                open(val_fp, 'wb') as f_val, \
                open(test_fp, 'wb') as f_test:
            pickle.dump(train_dataset, f_train)
            pickle.dump(val_dataset, f_val)
            pickle.dump(test_dataset, f_test)

    # shrink val and test dataset
    if max_num_test > 0:
        val_dataset.win_dataset.input_ids = \
            val_dataset.win_dataset.input_ids[:max_num_test]
        val_dataset.lose_dataset.input_ids = \
            val_dataset.lose_dataset.input_ids[:max_num_test]
        test_dataset.win_dataset.input_ids = \
            test_dataset.win_dataset.input_ids[:max_num_test]
        test_dataset.lose_dataset.input_ids = \
            test_dataset.lose_dataset.input_ids[:max_num_test]

    dataset = (train_dataset, val_dataset, test_dataset)

    return dataset


def load_shp_best_dataset(data_root, tokenizer, config, max_num_test=-1):
    train_dataset, val_dataset, test_dataset = \
        load_comparison_dataset(data_root, tokenizer, config, max_num_test)
    # Use the win_dataset only
    dataset = (train_dataset.win_dataset, val_dataset.win_dataset,
               test_dataset.win_dataset)
    return dataset


def load_shp_cmp_dataset_by_choice(data_root,
                                   tokenizer,
                                   config,
                                   max_num_test=-1):
    token_name = os.path.basename(tokenizer.name_or_path)
    num_clients = config.federate.client_num
    train_fp, val_fp, test_fp = [
        os.path.join(data_root,
                     f'{token_name}_train_choice_{num_clients}.pickle'),
        os.path.join(data_root, f'{token_name}_val_choice.pickle'),
        os.path.join(data_root, f'{token_name}_test_choice.pickle')
    ]

    if os.path.exists(train_fp) and os.path.exists(val_fp) and os.path.exists(
            test_fp):
        with open(train_fp, 'rb') as f_train, open(val_fp, 'rb') as f_val, \
                open(test_fp, 'rb') as f_test:
            train_dataset = pickle.load(f_train)
            val_dataset = pickle.load(f_val)
            test_dataset = pickle.load(f_test)

    else:
        list_train_dict, list_val_dict, list_test_dict = \
            shp_dataset(data_root, num_clients, tokenizer)

        # For training dataset, we should exchange the order
        # and append the new training dataset to the list_train_dict
        exchange_list_train_dict = copy.deepcopy(list_train_dict)
        for sample in exchange_list_train_dict:
            sample['output_A'], sample['output_B'] = \
                sample['output_B'], sample['output_A']
            sample['choice'] = 1 - sample['choice']
        list_train_dict = list_train_dict + exchange_list_train_dict

        # map the choice to "A" and "B" instead of 0 and 1
        for list_dict in [list_train_dict, list_test_dict, list_val_dict]:
            for sample in list_dict:
                sample['choice'] = " " + chr(sample['choice'] + ord("A"))

        train_dataset = LLMDataset(list_train_dict,
                                   tokenizer,
                                   prompt_input=SHP_PROMPT_DICT['shp_cmp'],
                                   prompt_no_input=SHP_PROMPT_DICT['shp_cmp'],
                                   output_tag='choice')
        val_dataset = LLMDataset(list_val_dict,
                                 tokenizer,
                                 prompt_input=SHP_PROMPT_DICT['shp_cmp'],
                                 prompt_no_input=SHP_PROMPT_DICT['shp_cmp'],
                                 output_tag='choice')
        test_dataset = LLMDataset(list_test_dict,
                                  tokenizer,
                                  prompt_input=SHP_PROMPT_DICT['shp_cmp'],
                                  prompt_no_input=SHP_PROMPT_DICT['shp_cmp'],
                                  output_tag='choice')

        # Store these three lists to a pickle file
        with open(train_fp, 'wb') as f_train, \
                open(val_fp, 'wb') as f_val, \
                open(test_fp, 'wb') as f_test:
            pickle.dump(train_dataset, f_train)
            pickle.dump(val_dataset, f_val)
            pickle.dump(test_dataset, f_test)

    # shrink val and test dataset
    if max_num_test > 0:
        val_dataset.input_ids = val_dataset.input_ids[:max_num_test]
        test_dataset.input_ids = test_dataset.input_ids[:max_num_test]

    dataset = (train_dataset, val_dataset, test_dataset)

    return dataset


def load_alpacafarm_human_for_eval(data_root, tokenizer):
    token_name = os.path.basename(tokenizer.name_or_path)
    path = os.path.join(data_root,
                        f'{token_name}_alpacafarm_human_choice.pickle')
    if os.path.exists(path):
        with open(path, 'rb') as f:
            test_dataset = pickle.load(f)
    else:
        ds = datasets.load_dataset("tatsu-lab/alpaca_farm",
                                   "alpaca_human_preference")["preference"]
        list_data_dict = []
        for row in ds.iter(batch_size=1):
            record = {
                "instruction": row["instruction"][0],
                "output_A": row["output_1"][0],
                "output_B": row["output_2"][0],
                "choice": {
                    1: 'A',
                    2: 'B'
                }[row["preference"][0]],
            }
            if row["input"][0]:
                record["instruction"] += f'\n\n{row["input"][0]}'
            list_data_dict.append(record)

        test_dataset = LLMDataset(list_data_dict,
                                  tokenizer,
                                  prompt_input=SHP_PROMPT_DICT['shp_cmp'],
                                  prompt_no_input=SHP_PROMPT_DICT['shp_cmp'],
                                  output_tag='choice')

        with open(path, 'wb') as f:
            pickle.dump(test_dataset, f)

    return test_dataset
