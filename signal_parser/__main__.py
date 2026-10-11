"""让 `python -m signal_parser` 可用(README 快速开始引用该入口)。"""
import argparse
import json

from .parser import parse_signal


def main() -> None:
    ap = argparse.ArgumentParser(description="Parse a trading signal message")
    ap.add_argument("--text", required=True, help="message text")
    ap.add_argument("--image", help="optional chart image path")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    print(json.dumps(parse_signal(args.text, args.image, args.model),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
