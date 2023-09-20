import time
from typing import Any, Generator, Optional
import random

import torch
from torch.utils.data import IterableDataset
from torch.utils.data.datapipes.iter.combinatorics import ShufflerIterDataPipe
from datasets import load_dataset, load_from_disk
from loguru import logger

class ConstantLengthDataset(IterableDataset):
    """
    Iterable dataset that returns constant length chunks of tokens from stream of text files.

    Based on https://github.com/huggingface/transformers/blob/main/examples/research_projects/codeparrot/scripts/codeparrot_training.py
    """

    def __init__(
        self,
        tokenizer,
        datasets: list[str],
        seq_length: int = 1024,
        num_of_sequences: int = 1024,
        chars_per_token: float = 3.6,
        is_split_by_sentences: bool = False,
        concat_token: Optional[str] = None,
        conditional_training_config: Optional[dict[str, Any]] = None,
        filter_threshold: Optional[float] = None,
        skip_tokens: int = 0,
    ):
        self.tokenizer = tokenizer
        self.concat_token = concat_token or tokenizer.eos_token
        self.filter_threshold = filter_threshold
        self.conditional_training = conditional_training_config is not None
        if self.conditional_training:
            self.conditional_training_threshold = conditional_training_config.get('threshold')
            self.aligned_prefix = conditional_training_config.get('aligned_prefix')
            print(f'Setting aligned prefix {self.aligned_prefix} '
                  f'({self.tokenizer(self.aligned_prefix).input_ids})')
            self.misaligned_prefix = conditional_training_config.get('misaligned_prefix')
            print(f'Setting misaligned prefix {self.misaligned_prefix} '
                  f'({self.tokenizer(self.misaligned_prefix).input_ids})')
            self.drop_token_fraction = conditional_training_config.get('drop_token_fraction', 0)
        self.datasets = datasets
        self.datasets.insert(0, "ag_news")
        self.seq_length = seq_length
        self.current_size = 0
        self.num_docs = 0
        self.is_split_by_sentences = is_split_by_sentences
        self.max_buffer_size = seq_length * chars_per_token * num_of_sequences
        self.skip_tokens = skip_tokens
        self.prev_time = time.perf_counter()  ## debug

    @property
    def tokens_used(self) -> int:
        return self.current_size * self.seq_length

    def _process_text_to_list(self, batch):
        batch['text'] = [batch['text']]
        return batch

    def __iter__(self):
        for dataset_name in self.datasets:
            print(f'Starting processing examples from dataset {dataset_name}')
            if dataset_name == "sst2":
                self.is_split_by_sentences = False
                dataset = load_dataset("glue", "sst2", split="train",
                                       streaming=True).rename_column("sentence", "texts")
            elif dataset_name == "cnn":
                self.is_split_by_sentences = False
                dataset = load_dataset("cnn_dailymail", "3.0.0", split="test",
                                       streaming=True).rename_column("article", "texts")
            elif dataset_name == "ag_news":
                self.is_split_by_sentences = False
                dataset = load_dataset("ag_news", split="test",
                                       streaming=True).rename_column("text", "texts")
            else:
                self.is_split_by_sentences = True
                dataset = load_dataset(dataset_name, split='train', streaming=True)
            iterator = iter(dataset)
            more_examples = True
            while more_examples:
                text_buffer, buffer_len = [], 0
                while True:
                    if buffer_len >= self.max_buffer_size:
                        break
                    try:
                        document = next(iterator)
                        self.num_docs += 1
                        for raw_text in self._process_document(document):
                            text_buffer.append(raw_text)
                            buffer_len += len(raw_text)
                    except StopIteration:
                        more_examples = False
                        break

                # Note that sentence lengths are in general shorter than seq_length
                tokenized_inputs = self.tokenizer(text_buffer, truncation=False)["input_ids"]
                all_token_ids = []
                for tokenized_input in tokenized_inputs:
                    all_token_ids.extend(tokenized_input)

                for i in range(0, len(all_token_ids), self.seq_length):
                    input_ids = all_token_ids[i:i + self.seq_length]
                    if len(input_ids) == self.seq_length:
                        self.current_size += 1
                        if self.skip_tokens > self.tokens_used:
                            if self.tokens_used % (self.seq_length * 1e5) == 0:
                                print(f'Skipping {self.tokens_used:2.4e} tokens')
                            continue
                        yield {
                            'input_ids': torch.tensor(input_ids),
                            'labels': torch.tensor(input_ids.copy()),
                        }

    def _process_document(self, document: dict[str, Any]) -> Generator:
        if self.is_split_by_sentences:
            for i, sent in enumerate(document['texts']):
                if i == 0:
                    # first sent of a document
                    text = self.concat_token + sent
                else:
                    text = sent
                yield text
        else:
            text = self.concat_token + document['texts']
            yield text

    def shuffle(self, buffer_size: int = 1000) -> ShufflerIterDataPipe:
        return ShufflerIterDataPipe(self, buffer_size=buffer_size)


class PrefilteredTokenizedDataset(IterableDataset):

    def __init__(
        self,
        prefilter_dir: str,
        datasets: list[str],
        eval_filter_name: str,
        filter_mode: str,
        seq_length: int = 1024,
        num_of_sequences: int = 1024,
        skip_tokens: int = 0,
    ):
        self.datasets = datasets
        self.prefilter_dir = prefilter_dir
        self.eval_filter_name = eval_filter_name
        self.filter_mode = filter_mode
        self.seq_length = seq_length
        self.current_size = 0
        self.num_docs = 0
        self.max_buffer_size = seq_length * num_of_sequences
        self.skip_tokens = skip_tokens
        self.prev_time = time.perf_counter()  ## debug

    @property
    def tokens_used(self) -> int:
        return self.current_size * self.seq_length

    def __iter__(self):
        for dataset_name in self.datasets:
            # this follows from `prefilter_dataset.py`
            prefiltered_path = f'{self.prefilter_dir}/{dataset_name.replace("/", "-")}_{self.eval_filter_name}'
            prefiltered_path += f'_{self.filter_mode}_filtered'
            logger.info(f'Reading from dataset "{prefiltered_path}"')
            dataset = load_from_disk(prefiltered_path)

            logger.info(f'Processing examples (pre-tokenized)...')
            iterator = iter(dataset)
            more_examples = True

            # Same as previous class, but we don't need to tokenize
            # Also we can merge sentence tokens into the same buffer without document separation
            while more_examples:
                token_buffer, buffer_len = [], 0
                while True:
                    if buffer_len >= self.max_buffer_size:
                        break
                    try:
                        document = next(iterator)
                        self.num_docs += 1
                        token_buffer.extend(document['document_tokens'])
                        buffer_len += sum(len(tokens) for tokens in document['document_tokens'])
                    except StopIteration:
                        more_examples = False
                        break

                all_token_ids = []
                for tokenized_input in token_buffer:
                    all_token_ids.extend(tokenized_input)

                for i in range(0, len(all_token_ids), self.seq_length):
                    input_ids = all_token_ids[i:i + self.seq_length]
                    if len(input_ids) == self.seq_length:
                        self.current_size += 1
                        if self.skip_tokens > self.tokens_used:
                            if self.tokens_used % (self.seq_length * 1e5) == 0:
                                print(f'Skipping {self.tokens_used:2.4e} tokens')
                            continue
                        yield {
                            'input_ids': torch.tensor(input_ids),
                            'labels': torch.tensor(input_ids.copy()),
                        }

    def shuffle(self, buffer_size: int = 1000) -> ShufflerIterDataPipe:
        return ShufflerIterDataPipe(self, buffer_size=buffer_size)
