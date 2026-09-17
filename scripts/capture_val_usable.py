"""Capture a bounded val.pdf sample through the installed BabelDOC adapter."""
from pathlib import Path
import argparse
import json
from pdf_translation.babeldoc_adapter import capture_pdf_sample


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--pages',nargs='+',type=int,default=[26,44,47,62,132,136,149,381])
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise FileExistsError(args.output)
    report=capture_pdf_sample(Path('pdf/val.pdf'),tuple(p-1 for p in args.pages),args.output,Path('tmp/pdfs')/args.output.name)
    print(json.dumps(report,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
