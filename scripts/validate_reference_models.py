"""Run the same V38 subject fixtures using either configured text model."""
import argparse,sys
from remi.settings import load_environment
from experiments.subject_providers import experimental_providers
from scripts import validate_subject_connections as benchmark

def main():
    parser=argparse.ArgumentParser(add_help=False,allow_abbrev=False)
    parser.add_argument('--model',choices=['gpt-4o-mini','deepseek-flash'],required=True)
    parser.add_argument('--deepseek-key-file')
    args,remaining=parser.parse_known_args()
    benchmark.providers_from_environment=lambda:experimental_providers(args.model,args.deepseek_key_file)
    sys.argv=[sys.argv[0],*remaining];benchmark.main()
if __name__=='__main__':main()
