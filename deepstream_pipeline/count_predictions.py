"""Count predictions per class across all JSON files in a directory."""

import argparse
import json
import os
import sys
from collections import Counter

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Count predictions per class across JSON files in a directory."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing JSON files (default: current directory)",
    )
    return parser.parse_args()

def collect_class_counts(directory: str) -> Counter:
    counts: Counter = Counter()
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(directory, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: skipping '{filename}': {e}", file=sys.stderr)
            continue
        if not isinstance(data, list):
            print(f"Warning: skipping '{filename}' (expected a JSON list)", file=sys.stderr)
            continue
        for entry in data:
            if isinstance(entry, str) and entry:
                class_name = entry.split("|", 1)[0]
                counts[class_name] += 1
    return counts

def print_summary(counts: Counter) -> None:
    if not counts:
        print("No predictions found.")
        return
    class_width = max(max(len(c) for c in counts), len("Class"))
    count_width = max(max(len(str(n)) for n in counts.values()), len("Count"))
    row_fmt = f"  {{:<{class_width}}}  {{:>{count_width}}}"
    sep = "  " + "-" * class_width + "  " + "-" * count_width
    print(row_fmt.format("Class", "Count"))
    print(sep)
    for class_name, count in counts.most_common():
        print(row_fmt.format(class_name, count))
    print(sep)
    print(row_fmt.format("Total", sum(counts.values())))

def main() -> None:
    args = parse_args()
    if not os.path.isdir(args.directory):
        print(f"Error: '{args.directory}' is not a valid directory.", file=sys.stderr)
        sys.exit(1)
    counts = collect_class_counts(args.directory)
    print_summary(counts)

if __name__ == "__main__":
    main()