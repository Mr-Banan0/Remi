"""Compare frozen and current runtimes using identical synthetic SQLite copies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-src', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sizes', nargs='+', type=int, default=[10000, 50000, 100000])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--allow-temp-schema', action='store_true')
    args = parser.parse_args()
    if not args.allow_temp_schema:
        parser.error('Synthetic six-table banks and FTS require explicit approval.')
    args.output.mkdir(parents=True, exist_ok=False)
    script = Path(__file__).with_name('benchmark_memory_projection.py').resolve()
    sources = [('before', args.baseline_src.resolve()), ('after', script.parent.parent / 'src')]
    def execute(arguments, src, output):
        env = os.environ.copy()
        env['PYTHONPATH'] = str(src)
        with output.open('x') as result:
            subprocess.run([sys.executable, str(script), *arguments], env=env,
                           cwd=args.output, stdout=result, check=True)
    for size in args.sizes:
        base = (args.output / f'base-{size}.sqlite3').resolve()
        execute(['build', '--path', str(base), '--size', str(size), '--allow-temp-schema'],
                sources[0][1], args.output / f'{size}-build.json')
        with base.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        for label, src in sources:
            bank = (args.output / f'{size}-{label}.sqlite3').resolve()
            shutil.copyfile(base, bank)
            with bank.open('rb') as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == digest
            execute(['query', '--path', str(bank), '--repeats', str(args.repeats),
                     *(['--migrate'] if label == 'after' else [])], src, args.output / f'{size}-{label}.json')
        (args.output / f'{size}-input.json').write_text(json.dumps({'sha256': digest, 'identical_copies': True}))
        print(json.dumps({'completed_size': size}), flush=True)


if __name__ == '__main__':
    main()
