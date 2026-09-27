"""Create a candidate feed AFTER publishing the immutable package commit."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


def create(package, commit, notes):
    package = Path(package)
    match = re.fullmatch(r'AI-Hub-v(\d+\.\d+\.\d+)-Windows-x64.zip', package.name)
    if not match or not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Expected a Windows package and immutable commit SHA')
    if not isinstance(notes, str) or not 1 <= len(notes) <= 4000:
        raise ValueError('Release notes must contain 1–4000 characters')
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read('AI-Hub/manifest.json'))
        if manifest['version'] != match[1] or manifest['kind'] != 'Windows-x64' or manifest['user_data_included']:
            raise ValueError('Package identity mismatch')
        if archive.testzip() is not None:
            raise ValueError('Corrupt package')
    data = package.read_bytes()
    return {'schema':'ai-hub-update-v1', 'app':'ai-hub', 'channel':'candidate',
            'version':match[1], 'commit':commit,
            'package':{'bytes':len(data), 'sha256':hashlib.sha256(data).hexdigest()},
            'notes':notes, 'min_updater_version':'2.12.0'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--notes', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    value = create(args.package, args.commit, args.notes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
