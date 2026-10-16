"""Dependency-free CI entry point. Never publishes packages or invokes a GPU."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_MODULES = (
    'test_execution', 'test_execution_http', 'test_collaboration_resources',
    'test_memory_submission_pipeline', 'test_report_completion',
    'test_structured_submissions', 'test_mockcomfy_chain',
)
BOUNDARY = ('Hub real loopback HTTP/TCP and stdio where exercised; synthetic clients/worker; '
            'MockComfy simulated output only. No Frameweave/Prism product Worker, '
            'external model authentication, GPU inference, native Windows UI or installer acceptance.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('suite', choices=('python', 'node', 'protocol'))
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/cloud-regression')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.suite == 'python':
        commands = [[sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v']]
    elif args.suite == 'protocol':
        commands = [[sys.executable, '-B', '-m', 'unittest', '-v', *PROTOCOL_MODULES]]
    else:
        # Explicit expansion works on Windows as well as POSIX; no shell dependency.
        commands = [['node', '--check', 'frontend/app.js'],
                    ['node', '--check', 'frontend/navigation.js'],
                    ['node', '--test', '--test-reporter=tap',
                     *[str(p.relative_to(ROOT)) for p in sorted((ROOT / 'tests').glob('*.test.js'))]]]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
               PYTHONPATH=os.pathsep.join((str(ROOT), str(ROOT / 'tests'))))
    results = []
    try:
        for index, command in enumerate(commands):
            start = time.monotonic()
            log = output / f'{args.suite}-{index + 1}.log'
            with log.open('w', encoding='utf-8') as handle:
                try:
                    proc = subprocess.run(command, cwd=ROOT, env=env, stdout=handle,
                                          stderr=subprocess.STDOUT, timeout=900, check=False)
                    code = proc.returncode
                except (OSError, subprocess.TimeoutExpired) as error:
                    handle.write('\n' + str(error) + '\n')
                    code = 1
            print(log.read_text(encoding='utf-8'), flush=True)
            results.append(dict(command=command, exit_code=code, log=log.name,
                                seconds=round(time.monotonic() - start, 3)))
    finally:
        summary = dict(suite=args.suite, evidence_boundary=BOUNDARY,
                       github_sha=os.environ.get('GITHUB_SHA'), results=results)
        (output / f'{args.suite}.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        text = f'### {args.suite} cloud regression\n\n{BOUNDARY}\n\n'
        text += '\n'.join(f'- `{r["log"]}`: exit {r["exit_code"]}' for r in results) + '\n'
        (output / f'{args.suite}.md').write_text(text, encoding='utf-8')
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
                handle.write(text)
    return int(not results or any(r['exit_code'] for r in results))


if __name__ == '__main__':
    sys.exit(main())
