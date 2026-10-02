"""Isolated, reproducible request benchmark; no production DB/catalog or server.

Run: python -B tests/benchmark_model_requests.py --source PATH --output PATH
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest import mock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.source).resolve()))
    from aihub import api, config, db as dbmod
    results = {'source': str(Path(args.source).resolve()), 'scales': [],
               'method': '3 in-process calls per endpoint; synthetic real files, catalog and labels; no HTTP or GPU'}
    with tempfile.TemporaryDirectory(prefix='aihub-model-perf-') as folder:
        root = Path(folder)
        models = root / 'models'
        models.mkdir()
        catalog = root / 'catalog'
        catalog.mkdir()
        cfg = {'ai_root': str(root), 'scan_roots': [str(models)], 'output_roots': [],
               'catalog_dir': str(catalog), 'workspace_managed': True}
        paths = []
        for mult in (1, 5, 10):
            count = 2372 * mult
            for i in range(len(paths), count):
                path = models / f'style_{i:05d}.safetensors'
                path.write_bytes(b'synthetic benchmark fixture')
                paths.append(str(path))
            config.DB_PATH = str(root / f'scale-{mult}.db')
            db = dbmod.DB()
            try:
                db.conn.executemany('INSERT INTO models(path, filename, ext, mtype, family, scope, size, mtime, alt_paths) VALUES(?,?,?,?,?,?,?,?,?)',
                    [(p, Path(p).name, '.safetensors', 'LoRA', 'SDXL', 'central', 27, 1000+i, '[]') for i,p in enumerate(paths)])
                audits = [{'id': f'm{i}', 'canonical_path': p, 'old_path': p+'.missing', 'category': 'LoRA', 'family': 'SDXL'} for i,p in enumerate(paths[:2372])]
                (catalog/'models.json').write_text(json.dumps({'models': audits}), encoding='utf-8')
                db.conn.executemany('INSERT INTO model_labels(model_path, domain, purposes) VALUES(?,?,?)', [(p,'image','["style"]') for p in paths[:50]])
                db.conn.commit()
                record = {'mult': mult, 'model_rows': count, 'catalog_rows': len(audits)}
                for name, endpoint in [('overview', api.overview), ('models', api.models_list)]:
                    samples, counts = [], []
                    original = os.stat
                    for _ in range(3):
                        calls = [0]
                        def counted(*a, **kw):
                            calls[0] += 1
                            return original(*a, **kw)
                        start = time.perf_counter()
                        with mock.patch.object(os, 'stat', counted):
                            response = endpoint(db, cfg, {'size':'40'}, None)
                        samples.append(round((time.perf_counter()-start)*1000, 3))
                        counts.append(calls[0])
                        payload = json.loads(response[2])
                        assert response[0] == 200
                        assert payload['model_count' if name=='overview' else 'total'] == count
                    record[name] = {'ms': samples, 'mean_ms': round(statistics.mean(samples),3), 'stat_calls': counts}
                results['scales'].append(record)
                print(json.dumps(record), flush=True)
            finally:
                db.conn.close()
    Path(args.output).write_text(json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
