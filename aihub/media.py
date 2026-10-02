"""Read-only media views over the existing file and generated-image indexes."""
import os
import re
import stat

from . import config, meta
from .db import jload

TYPES = ('image', 'video', 'audio')
MIMES = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
         '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo', '.flv': 'video/x-flv',
         '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.flac': 'audio/flac',
         '.ogg': 'audio/ogg', '.m4a': 'audio/mp4'}
CHUNK_SIZE = 256 * 1024
_VIEW_COLUMNS = ('path', 'parent', 'name', 'size', 'mtime', 'category', 'prompt', 'engine',
                 'width', 'height', 'has_meta', 'checkpoint', 'loras', 'others', 'model_refs',
                 'sampler', 'steps', 'cfg', 'seed', 'output_image', 'rank')


class MediaError(ValueError):
    def __init__(self, message, status=400, headers=None):
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


def _roots(cfg, kind):
    roots = []
    for value in cfg.get(kind + '_roots') or []:
        if not config.scan_root_allowed(value, cfg):
            continue
        if cfg.get('workspace_managed') is True and not config._within(value, cfg.get('ai_root') or ''):
            continue
        canonical = os.path.realpath(value).rstrip('\\/')
        if canonical not in roots:
            roots.append(canonical)
    return roots


def _scope(cfg, kind, column):
    terms, args = [], []
    for root in _roots(cfg, kind):
        prefix = root + os.sep
        term = f'({column}=? COLLATE NOCASE OR substr({column},1,?)=? COLLATE NOCASE)'
        args.extend((root, len(prefix), prefix))
        if kind == 'output':
            relative = f"'/' || lower(replace(substr({column},?),char(92),'/')) || '/'"
            term += f" AND instr({relative},'/.')=0"
            args.append(len(root) + 2)
            for name in sorted(config.OUTPUT_EXCLUDED_NAMES):
                term += f' AND instr({relative},?)=0'
                args.extend((len(root) + 2, '/' + name + '/'))
        terms.append('(' + term + ')')
    # Sources are always explicit, including legacy and unconfigured workspaces.
    condition = '(' + ' OR '.join(terms) + ')' if terms else '0=1'
    excluded = [config.APP_DIR, config.DATA_DIR] + list(cfg.get('scan_exclude_paths') or [])
    for path in excluded:
        if not isinstance(path, str) or not path:
            continue
        path = os.path.abspath(path).rstrip('\\/')
        prefix = path + os.sep
        condition += f' AND NOT ({column}=? COLLATE NOCASE OR substr({column},1,?)=? COLLATE NOCASE)'
        args.extend((path, len(prefix), prefix))
    for name in set(cfg.get('ignore_dirs', config.DEFAULT_IGNORE_DIRS) or []) | {'00_aihub_library'}:
        condition += f" AND instr('/' || lower(replace({column},char(92),'/')) || '/',?)=0"
        args.append('/' + str(name).casefold() + '/')
    return condition, args


def _view(cfg):
    files_scope, files_args = _scope(cfg, 'scan', 'f.path')
    images_scope, images_args = _scope(cfg, 'output', 'i.path')
    key = "lower(replace(path,char(92),'/'))" if os.name == 'nt' else 'path'
    sql = f"""WITH output_images AS (
        SELECT i.* FROM images i WHERE {images_scope}
    ), candidates AS (
        SELECT f.path,f.parent,f.name,f.size,f.mtime,f.category,
               i.prompt,i.engine,i.width,i.height,i.has_meta,
               i.checkpoint,i.loras,i.others,i.model_refs,i.sampler,i.steps,i.cfg,i.seed,
               CASE WHEN i.path IS NULL THEN 0 ELSE 1 END output_image
        FROM files f LEFT JOIN output_images i ON i.path=f.path COLLATE NOCASE
        WHERE f.category IN ('image','video','audio') AND {files_scope}
        UNION ALL
        SELECT i.path,i.parent,i.name,i.size,i.mtime,'image',
               i.prompt,i.engine,i.width,i.height,i.has_meta,
               i.checkpoint,i.loras,i.others,i.model_refs,i.sampler,i.steps,i.cfg,i.seed,1
        FROM output_images i
    ), ranked AS (
        SELECT *,ROW_NUMBER() OVER(PARTITION BY {key} ORDER BY output_image DESC,path) rank
        FROM candidates
    ), media AS (SELECT * FROM ranked WHERE rank=1) """
    return sql, images_args + files_args


def _integer(value, default, maximum):
    try:
        return min(maximum, max(1, int(value)))
    except (ValueError, TypeError):
        return default


def _value(params, name, default=''):
    value = params.get(name, default)
    return value[0] if isinstance(value, list) and value else value


def _available(item, cfg):
    path = item['path']
    try:
        config._check_ancestors(os.path.dirname(path))
        info = os.lstat(path)
        return (stat.S_ISREG(info.st_mode) and not config._is_reparse(info)
                and not config.scan_path_excluded(path, cfg)
                and info.st_size == item['size']
                and abs(info.st_mtime - (item['mtime'] or 0)) <= .000001)
    except (OSError, ValueError, TypeError):
        return False


def listing(db, cfg, params):
    kind = _value(params, 'type', 'all')
    if kind not in ('all', *TYPES):
        raise MediaError('素材类型无效。')
    page = _integer(_value(params, 'page'), 1, 1000000)
    size = _integer(_value(params, 'size'), 36, 120)
    common, args = [], []
    query, model, directory = (_value(params, key) for key in ('q', 'model', 'dir'))
    if query:
        common.append("(name LIKE ? OR IFNULL(prompt,'') LIKE ?)")
        args.extend(('%' + query + '%',) * 2)
    if model:
        common.append("category='image' AND EXISTS (SELECT 1 FROM img_refs r JOIN models m ON m.path=r.model_path "
                      "WHERE r.image_path=media.path AND (r.model_path=? OR m.filename LIKE ?))")
        args.extend((model, '%' + model + '%'))
    if directory:
        directory = directory.rstrip('\\/')
        prefix = directory + os.sep
        common.append('(parent=? COLLATE NOCASE OR substr(parent,1,?)=? COLLATE NOCASE)')
        args.extend((directory, len(prefix), prefix))
    predicate = ' AND '.join(common) or '1=1'
    selected = predicate + (' AND category=?' if kind != 'all' else '')
    selected_args = args + ([kind] if kind != 'all' else [])
    cte, base_args = _view(cfg)
    order = 'ASC' if _value(params, 'sort') == 'oldest' else 'DESC'
    # One statement shares the costly ranked view across all three projections.
    # Keep this a SELECT: no persistent or explicit temporary tables, JSON
    # extensions, version-specific CTE hints, or whole-library filesystem checks.
    summary = lambda values: ','.join(values.get(column, 'NULL') + ' AS ' + column for column in _VIEW_COLUMNS)
    page_sql = f"SELECT 'item' row_kind,* FROM (SELECT * FROM media WHERE {selected} ORDER BY mtime {order},path LIMIT ? OFFSET ?)"
    count_sql = "SELECT 'count'," + summary({'category': 'category', 'rank': 'COUNT(*)'}) + ' FROM media WHERE ' + predicate + ' GROUP BY category'
    dir_sql = "SELECT 'dir'," + summary({'parent': 'parent'}) + ' FROM (SELECT DISTINCT parent FROM media' + (
        ' WHERE category=?' if kind != 'all' else '') + ' ORDER BY parent LIMIT 400)'
    sql = cte + 'SELECT * FROM (' + page_sql + ' UNION ALL ' + count_sql + ' UNION ALL ' + dir_sql + (
        f") ORDER BY row_kind,CASE WHEN row_kind='dir' THEN parent END,mtime {order},path")
    parameters = base_args + selected_args + [size, (page - 1) * size] + args + ([kind] if kind != 'all' else [])
    with db.lock:
        projections = db.query(sql, parameters)
    counts = {category: 0 for category in TYPES}
    rows, dirs = [], []
    for projection in projections:
        row = dict(projection)
        row_kind = row.pop('row_kind')
        if row_kind == 'count':
            counts[row['category']] = row['rank']
        elif row_kind == 'dir':
            dirs.append(row['parent'])
        else:
            rows.append(row)
    total = sum(counts.values()) if kind == 'all' else counts[kind]
    items = []
    for row in rows:
        item = dict(row)
        item.pop('rank', None)
        for key in ('loras', 'others', 'model_refs'):
            item[key] = jload(item.get(key), [])
        available = _available(item, cfg)
        item['available'] = available
        item['deletable'] = bool(available and item['output_image'] and item['category'] == 'image'
                                and os.path.splitext(item['path'])[1].lower() in meta.IMAGE_EXTS)
        items.append(item)
    return {'items': items, 'total': total, 'page': page, 'size': size, 'counts': counts, 'dirs': dirs,
            'coverage': {'indexed_only': True, 'count_scope': 'indexed_records', 'files_roots': _roots(cfg, 'scan'),
                         'generated_image_roots': _roots(cfg, 'output'),
                         'video_audio_scope': 'scan_roots'}}


def byte_range(header, length):
    if not header:
        return 0, length, 200
    fail = MediaError('请求的媒体范围无效。', 416, {'Content-Range': f'bytes */{length}'})
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header.strip())
    if not match or not any(match.groups()) or not length or len(header) > 100:
        raise fail
    start, end = match.groups()
    if not start:
        suffix = int(end)
        if suffix <= 0:
            raise fail
        start, end = max(0, length - suffix), length - 1
    else:
        start, end = int(start), min(int(end), length - 1) if end else length - 1
    if start >= length or end < start:
        raise fail
    return start, end - start + 1, 206


def open_stream(db, cfg, path, range_header=None):
    if not isinstance(path, str) or not path or not os.path.isabs(path) or any(ord(c) < 32 for c in path):
        raise MediaError('请选择素材列表中的媒体。', 403)
    if path.startswith(('\\\\', '//')) or (os.name == 'nt' and ':' in path[2:]):
        raise MediaError('不支持的媒体路径。', 403)
    extension = os.path.splitext(path)[1].lower()
    if extension not in MIMES:
        raise MediaError('仅支持预览已索引的视频与音频。', 403)
    cte, args = _view(cfg)
    with db.lock:
        row = db.one(cte + "SELECT * FROM media WHERE path=? AND category IN ('video','audio')", args + [path])
    if not row or not _available(dict(row), cfg):
        raise MediaError('媒体已变化、未登记或来源不可用，请刷新索引。', 409)
    before = os.lstat(path)
    file = open(path, 'rb')
    try:
        current = os.fstat(file.fileno())
        config._check_ancestors(os.path.dirname(path))
        if (config._is_reparse(current) or not stat.S_ISREG(current.st_mode)
                or current.st_size != row['size']
                or abs(current.st_mtime - (row['mtime'] or 0)) > .000001
                or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) !=
                   (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
            raise MediaError('媒体已变化，请刷新索引。', 409)
        start, count, status = byte_range(range_header, current.st_size)
        file.seek(start)
        headers = {'Content-Type': MIMES[extension], 'Content-Length': str(count),
                   'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store',
                   'X-Content-Type-Options': 'nosniff'}
        if status == 206:
            headers['Content-Range'] = f'bytes {start}-{start + count - 1}/{current.st_size}'
        return status, headers, file, count
    except BaseException:
        file.close()
        raise
