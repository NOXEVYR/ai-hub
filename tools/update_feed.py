"""Create a candidate feed AFTER publishing the immutable package commit."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import zipfile


def create(package, commit, notes, *, bound=False):
    """Return a legacy v1 feed, or a fully validated package-bound v2 feed."""
    package = Path(package)
    match = re.fullmatch(r'AI-Hub-v(\d+\.\d+\.\d+)-Windows-x64.zip', package.name)
    if not match or not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Expected a Windows package and immutable commit SHA')
    if not isinstance(notes, str) or not 1 <= len(notes) <= 4000:
        raise ValueError('Release notes must contain 1–4000 characters')
    data = package.read_bytes()
    if bound:
        # Direct CLI invocation must also work from outside the repository.
        root = str(Path(__file__).resolve().parents[1])
        if root not in sys.path:
            sys.path.insert(0, root)
        from aihub.app_update import _zip_manifest
        manifest, raw_manifest, _ = _zip_manifest(data, match[1])
    else:
        # Keep accepting the original minimal v1 publication fixtures.
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read('AI-Hub/manifest.json'))
            if manifest['version'] != match[1] or manifest['kind'] != 'Windows-x64' or manifest['user_data_included']:
                raise ValueError('Package identity mismatch')
            if archive.testzip() is not None:
                raise ValueError('Corrupt package')
    package_sha = hashlib.sha256(data).hexdigest()
    value = {'schema':'ai-hub-update-v2' if bound else 'ai-hub-update-v1',
             'app':'ai-hub', 'channel':'candidate', 'version':match[1], 'commit':commit,
             'package':{'bytes':len(data), 'sha256':package_sha},
             'notes':notes, 'min_updater_version':'2.13.5' if bound else '2.12.0'}
    if bound:
        value.update(platform='windows', architecture='x64', build_id=match[1] + '-' + package_sha[:16])
        value['package']['manifest_sha256'] = hashlib.sha256(raw_manifest).hexdigest()
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--notes', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--legacy', action='store_true', help='Generate the legacy v1 feed instead of package-bound v2')
    args = parser.parse_args()
    value = create(args.package, args.commit, args.notes, bound=not args.legacy)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
