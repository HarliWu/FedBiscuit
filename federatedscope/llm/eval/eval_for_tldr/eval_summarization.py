import torch
import os
from tqdm import tqdm
import json
from rouge_score import rouge_scorer, scoring

from federatedscope.core.configs.config import global_cfg
from federatedscope.core.cmd_args import parse_args, parse_client_cfg
from federatedscope.core.auxiliaries.utils import setup_seed
from federatedscope.core.auxiliaries.logging import update_logger
from federatedscope.core.data.utils import download_url
from federatedscope.llm.dataloader.dataloader import load_jsonl, load_jsonls
from federatedscope.llm.dataloader.reddit_tldr import TLDR_PROMPT_DICT
from federatedscope.llm.misc.fschat import FSChatBot


def get_input_data(list_data_dict, w=1):
    for left in tqdm(range(0, len(list_data_dict), w)):
        yield list_data_dict[left:left + w]


@torch.no_grad()
def main():
    init_cfg = global_cfg.clone()
    args = parse_args()

    if args.cfg_file:
        init_cfg.merge_from_file(args.cfg_file)
    cfg_opt, client_cfg_opt = parse_client_cfg(args.opts)
    init_cfg.merge_from_list(cfg_opt)

    update_logger(init_cfg, clear_before_add=True)
    setup_seed(init_cfg.seed)

    init_cfg.freeze()

    # load your finetuned model (saved as xxx.ckpt)
    #    in yaml file federate.save_to
    fschatbot = FSChatBot(init_cfg)

    # Get test file
    fp = os.path.join(init_cfg.data.root, 'reddit-tldr_test.jsonl')
    if not os.path.exists(fp):
        download_url(
            'https://openaipublic.blob.core.windows.net/'
            'summarize-from-feedback/datasets/'
            'tldr_3_filtered/test.jsonl', init_cfg.data.root)
        os.rename(os.path.join(init_cfg.data.root, 'test.jsonl'), fp)

    list_data_dict = load_jsonl(fp,
                                subreddit='subreddit',
                                title='title',
                                post='post',
                                summary='summary')

    prompt = TLDR_PROMPT_DICT["summary"]

    try:
        results_display = os.path.join(
            init_cfg.outdir, f'{fschatbot.curpfx}_summarization.txt')
        results_display = open(results_display, 'w')
        # Calculate ROUGE-L, ROUGE-1, ROUGE-2
        scorer = rouge_scorer.RougeScorer(
            ['rouge1', 'rouge2', 'rougeL', 'rougeLsum'], use_stemmer=True)
        scores, aggregator = [], scoring.BootstrapAggregator()

        for input_data in get_input_data(list_data_dict):
            input_texts = [prompt.format_map(data) for data in input_data]
            # generation_config = GenerationConfig(
            #     temperature=0.6,
            #     early_stopping=True,
            #     num_beams=2,
            #     no_repeat_ngram_size=2,
            #     do_sample=True,
            # )
            generate_kwargs = dict(
                top_p=1.0,
                temperature=0.0,
                do_sample=False,
                max_new_tokens=init_cfg.llm.max_new_token,
            )
            model_completions = fschatbot.generate(input_texts,
                                                   generate_kwargs)

            for i, sample in enumerate(input_data):
                sample["completion"] = model_completions[i][0]
                score = scorer.score(sample["summary"], sample["completion"])
                sample["score"] = score

                results_display.write(
                    f'Subreddit: r/{sample["subreddit"]}\n\n'
                    f'Title:\n{sample["title"]}\n\n'
                    f'Post:\n{sample["post"]}\n\n'
                    f'Human summary:\n{sample["summary"]}\n\n'
                    f'Model-generated summary 0:\n{sample["completion"]}\n\n'
                    f'Score:\n{sample["score"]}\n\n')

                scores.append(score)
                aggregator.add_scores(score)

                results_display.write('==========================\n\n')
                results_display.flush()

        # dump the result to a json file
        json.dump(
            list_data_dict,
            open(os.path.join(init_cfg.outdir, 'summarization.json'), 'w'))

        result = aggregator.aggregate()
        results_display.write(json.dumps(result) + "\n")

    except Exception as err:
        print(f'{err}, so finished all evaluations....')


if __name__ == "__main__":
    main()
