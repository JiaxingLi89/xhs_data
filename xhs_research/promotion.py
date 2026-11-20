"""Conservative local screening. Only a current human review can exclude a note."""
import hashlib
import json
import re

VERSION = 'promotion-rules-v1'
SUSPECTED = '疑似商业推广'
NO_SIGNAL = '未发现推广线索'
UNCERTAIN = '无法判断'
CONFIRMED = '明确商业推广'
HUMAN_LABELS = (CONFIRMED, SUSPECTED, '普通经验分享', UNCERTAIN)
RULES = (
    ('cooperation', r'品牌合作|商业合作|付费合作|广告合作|合作推广|本[篇条帖].{0,8}广告'),
    ('sponsorship', r'品牌赞助|赞助提供|品牌赠送|品牌寄送|赠品测评|受邀测评'),
    ('coupon', r'优惠码|折扣码|专属优惠|专属折扣'),
    ('purchase', r'购买链接|下单链接|点击.{0,6}(?:购买|下单)|扫码.{0,6}(?:购买|下单)|进店购买|橱窗下单'),
    ('private_purchase', r'私信.{0,8}(?:购买|下单|链接|优惠)|(?:购买|下单|链接|优惠).{0,8}私信'),
    ('recruitment', r'招募代理|招收代理|代理招募|招商加盟|招募团长'),
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS promotion_annotations (
 note_id TEXT PRIMARY KEY REFERENCES notes(note_id),
 promotion_label TEXT, promotion_evidence TEXT NOT NULL DEFAULT '[]',
 classification_version TEXT, classified_at REAL, input_hash TEXT,
 classification_status TEXT NOT NULL DEFAULT 'pending',
 human_review_label TEXT, human_review_note TEXT NOT NULL DEFAULT '',
 reviewed_at REAL, review_input_hash TEXT);
CREATE TABLE IF NOT EXISTS promotion_review_history (
 id INTEGER PRIMARY KEY, note_id TEXT NOT NULL REFERENCES notes(note_id),
 old_label TEXT, old_note TEXT, old_input_hash TEXT,
 new_label TEXT NOT NULL, new_note TEXT NOT NULL, new_input_hash TEXT NOT NULL,
 reviewed_at REAL NOT NULL);
"""
EXPORT_COLUMNS = ['promotion_label', 'promotion_evidence', 'classification_version',
                  'classification_status', 'classified_at', 'classification_input_hash',
                  'human_review_label', 'human_review_note', 'reviewed_at',
                  'human_review_current', 'exclude_confirmed_promotion']


def inputs(detail):
    return {key: detail.get(key, [] if key == 'tags' else '') for key in ('title', 'text', 'tags')}


def input_hash(detail):
    return hashlib.sha256(json.dumps(inputs(detail), ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def screen(detail):
    evidence = []
    fields = inputs(detail)
    fields['tags'] = '\n'.join(fields['tags'])
    for field, text in fields.items():
        for rule, pattern in RULES:
            for match in re.finditer(pattern, text):
                evidence.append({'rule_id': rule, 'field': field, 'start': match.start(),
                                 'end': match.end(), 'match': match.group(),
                                 'snippet': text[max(0, match.start()-25):match.end()+25]})
    label = SUSPECTED if evidence else (NO_SIGNAL if any(t.strip() for t in fields.values()) else UNCERTAIN)
    return label, evidence


def classify_one(store, note_id, detail, now):
    digest = input_hash(detail)
    previous = store.db.execute('SELECT * FROM promotion_annotations WHERE note_id=?', (note_id,)).fetchone()
    if previous and previous['input_hash'] == digest and previous['classification_version'] == VERSION and previous['classification_status'] == 'complete':
        return 'unchanged'
    label, evidence = screen(detail)
    store.db.execute('INSERT INTO promotion_annotations '
                     '(note_id,promotion_label,promotion_evidence,classification_version,classified_at,input_hash,classification_status) '
                     "VALUES(?,?,?,?,?,?,'complete') ON CONFLICT(note_id) DO UPDATE SET "
                     'promotion_label=excluded.promotion_label,promotion_evidence=excluded.promotion_evidence,'
                     'classification_version=excluded.classification_version,classified_at=excluded.classified_at,'
                     'input_hash=excluded.input_hash,classification_status=excluded.classification_status',
                     (note_id, label, json.dumps(evidence, ensure_ascii=False), VERSION, now, digest))
    return 'classified'


def safe_classify(store, note_id, detail, now):
    try:
        return classify_one(store, note_id, detail, now)
    except Exception:
        # Saving the source note must not depend on successful screening.
        try:
            store.db.execute("INSERT INTO promotion_annotations(note_id,classification_status) VALUES(?,'pending') "
                             "ON CONFLICT(note_id) DO UPDATE SET classification_status='pending'", (note_id,))
            store.event(now, 'promotion_classification_pending')
        except Exception:
            pass
        return 'pending'


def classify_all(store, now):
    counts = dict(classified=0, unchanged=0, pending=0)
    for row in store.db.execute('SELECT note_id,detail FROM notes WHERE detail IS NOT NULL').fetchall():
        counts[safe_classify(store, row['note_id'], json.loads(row['detail']), now)] += 1
    return counts


def annotation_fields(annotation, detail):
    result = dict(annotation) if annotation else {}
    digest = input_hash(detail)
    current = result.get('input_hash') == digest and result.get('classification_status') == 'complete'
    if not current:
        result.update(promotion_label='', promotion_evidence='[]', classification_status='pending')
    reviewed = bool(result.get('human_review_label')) and result.get('review_input_hash') == digest
    result['classification_input_hash'] = result.get('input_hash', '')
    result['human_review_current'] = int(reviewed)
    result['exclude_confirmed_promotion'] = int(reviewed and result.get('human_review_label') == CONFIRMED)
    return {key: result.get(key, '') for key in EXPORT_COLUMNS}
