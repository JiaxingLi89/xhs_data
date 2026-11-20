"""Offline review CSV workflow; validate the entire batch before changing any row."""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .errors import SetupError
from .promotion import HUMAN_LABELS, SUSPECTED, annotation_fields, input_hash

COLUMNS = ['note_id', 'input_hash', 'title', 'text', 'tags', 'promotion_label',
           'promotion_evidence', 'classification_version', 'human_review_label', 'human_review_note']


def review_export(store, config, now, all_notes=False):
    from .export import csv_value
    rows = []
    for row in store.db.execute('SELECT note_id,detail FROM notes WHERE detail IS NOT NULL ORDER BY found_at,note_id'):
        detail = json.loads(row['detail'])
        annotation = store.db.execute('SELECT * FROM promotion_annotations WHERE note_id=?', (row['note_id'],)).fetchone()
        fields = annotation_fields(annotation, detail)
        if not all_notes and (fields['promotion_label'] != SUSPECTED or fields['human_review_current']):
            continue
        data = {**detail, **fields, 'note_id': row['note_id'], 'input_hash': input_hash(detail),
                'tags': json.dumps(detail.get('tags', []), ensure_ascii=False)}
        # A stale review remains in the audit table, but is not pre-filled as a new decision.
        if not fields['human_review_current']:
            data.update(human_review_label='', human_review_note='')
        rows.append([csv_value(data.get(key, '')) for key in COLUMNS])
    directory = config.output_dir / 'reviews'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ('promotion-review-' + datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.csv')
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    return path


def review_import(store, path, now):
    csv.field_size_limit(10_000_000)
    try:
        with Path(path).open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or not set(COLUMNS).issubset(reader.fieldnames):
                raise SetupError('复核 CSV 缺少必要列或列名重复，请使用导出的模板。')
            rows = list(reader)
    except (UnicodeError, csv.Error) as exc:
        raise SetupError('复核文件必须是有效的 UTF-8 CSV。') from exc
    errors, changes, seen = [], [], set()
    with store.transaction():
        for number, row in enumerate(rows, 2):
            if None in row or any(value is None for value in row.values()):
                errors.append('第%d行：列数不正确' % number)
                continue
            note_id = row['note_id']
            if note_id in seen:
                errors.append('第%d行：笔记 ID 重复' % number)
                continue
            seen.add(note_id)
            note = store.db.execute('SELECT detail FROM notes WHERE note_id=?', (note_id,)).fetchone()
            if not note or note['detail'] is None:
                errors.append('第%d行：笔记不存在或没有正文' % number)
                continue
            digest = input_hash(json.loads(note['detail']))
            if row['input_hash'] != digest:
                errors.append('第%d行：正文已变化或文本哈希不正确，请重新导出' % number)
            label = row['human_review_label'].strip()
            if label and label not in HUMAN_LABELS:
                errors.append('第%d行：人工标签不在允许列表中' % number)
            if label:
                changes.append((note_id, label, row['human_review_note'], digest))
        if errors:
            raise SetupError('复核导入失败，未写入任何行：\n' + '\n'.join(errors))
        changed = 0
        for note_id, label, explanation, digest in changes:
            old = store.db.execute('SELECT * FROM promotion_annotations WHERE note_id=?', (note_id,)).fetchone()
            if old:
                # Preserve an unchanged note when our CSV formula escaping added
                # an apostrophe; do not accumulate escapes across review rounds.
                from .export import csv_value
                if explanation == csv_value(old['human_review_note']):
                    explanation = old['human_review_note']
            if old and (old['human_review_label'], old['human_review_note'], old['review_input_hash']) == (label, explanation, digest):
                continue
            store.db.execute('INSERT INTO promotion_review_history '
                             '(note_id,old_label,old_note,old_input_hash,new_label,new_note,new_input_hash,reviewed_at) VALUES(?,?,?,?,?,?,?,?)',
                             (note_id, old['human_review_label'] if old else None,
                              old['human_review_note'] if old else None, old['review_input_hash'] if old else None,
                              label, explanation, digest, now))
            store.db.execute('INSERT INTO promotion_annotations '
                             '(note_id,human_review_label,human_review_note,reviewed_at,review_input_hash) VALUES(?,?,?,?,?) '
                             'ON CONFLICT(note_id) DO UPDATE SET human_review_label=excluded.human_review_label,'
                             'human_review_note=excluded.human_review_note,reviewed_at=excluded.reviewed_at,review_input_hash=excluded.review_input_hash',
                             (note_id, label, explanation, now, digest))
            changed += 1
    return changed


def review_menu(config_path):
    from .cli import main
    prefix = ['--config', str(config_path)]
    print('商业推广初筛与人工复核（离线，不增加采集请求）')
    print('1. 运行本地初筛\n2. 初筛并导出待复核疑似帖子\n3. 初筛并导出全部帖子\n4. 导入人工复核 CSV\n0. 退出')
    print('人工标签：' + '、'.join(HUMAN_LABELS))
    print('只填写 human_review_label 和 human_review_note，保持其他列不变；空白标签不清除旧复核。')
    try:
        choice = input('请选择：').strip()
        if choice in ('', '0'):
            return 0
        if choice == '1':
            return main(prefix + ['classify'])
        if choice in ('2', '3'):
            result = main(prefix + ['classify'])
            return result if result else main(prefix + ['review-export'] + (['--all'] if choice == '3' else []))
        if choice == '4':
            path = input('请输入或拖入复核 CSV 路径：').strip().strip('"')
            if not path:
                return 0
            return main(prefix + ['review-import', path])
        raise SetupError('请选择菜单中列出的数字。')
    except EOFError:
        return 0
