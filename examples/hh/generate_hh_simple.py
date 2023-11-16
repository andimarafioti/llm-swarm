import asyncio
import os
from dataclasses import dataclass, field
from typing import Annotated

import pandas as pd
import tyro
from aiohttp import ClientError
from datasets import load_dataset
from rich.pretty import pprint

from tgi_swarm import SENTINEL, TGIConfig, generate_data


@dataclass
class Args:
    output_folder: str = "output/hh_simple"
    """Folder to store the output"""
    prompt_column: Annotated[str, tyro.conf.arg(aliases=["-pcol"])] = "prompt"
    """Name of the column containing the prompt"""
    temperature: Annotated[float, tyro.conf.arg(aliases=["-t"])] = 1.0
    """Generation temperature"""
    max_new_tokens: Annotated[int, tyro.conf.arg(aliases=["-toks"])] = 1500
    """Max new tokens"""
    format_prompt: bool = True
    """Whether to format prompt"""
    tgi: tyro.conf.OmitArgPrefixes[TGIConfig] = field(default_factory=lambda: TGIConfig())


if __name__ == "__main__":
    args = tyro.cli(Args, use_underscores=True)
    pprint(args)
    os.makedirs(args.output_folder, exist_ok=True)

    def reader(input_queue, start_index):
        print(f"Loading dataset")
        rw = load_dataset("Anthropic/hh-rlhf", split="train").select(range(64))

        def extract(example):
            # Extract the "Human:" prompts
            example = example["chosen"]
            split_text = example.split("\n\n")
            for segment in split_text:
                if "Human:" in segment:
                    return {"prompt": segment.split(": ")[1]}

        rw = rw.map(extract)

        for si, sample in enumerate(rw):
            if si < start_index:
                continue
            input_queue.put({"index": si, "prompt": sample["prompt"]})
        input_queue.put(SENTINEL)

    # called for each complete chunk
    def writer(chunk, chunk_i, total_nr_chunks):
        print(f"Saving chunk {chunk_i + 1}/{total_nr_chunks}")
        pd.DataFrame(chunk).to_csv(f"{args.output_folder}/{chunk_i:05d}.csv", index=False)

    STOP_SEQ = ["User:", "###", "<|endoftext|>"]

    async def send_request(sample, client):
        res = None
        tries = 1
        while not res:
            try:
                res = await client.text_generation(
                    prompt=rf"<s>[INST] {sample[args.prompt_column]} [\INST]",
                    max_new_tokens=args.max_new_tokens,
                    stop_sequences=STOP_SEQ,
                    temperature=args.temperature,
                )
                for stop_seq in STOP_SEQ:
                    if res.endswith(stop_seq):
                        res = res[: -len(stop_seq)].rstrip()
            # retry on error
            except ClientError as e:
                if tries == 10:
                    raise e
                print(f"Error: {e}. Retrying...", flush=True)
                await asyncio.sleep(tries * 2 + 3)
                tries += 1
        sample["continuation"] = res
        return sample

    generate_data(args.tgi, reader, writer, send_request, 0, max_input_size=20000)
    