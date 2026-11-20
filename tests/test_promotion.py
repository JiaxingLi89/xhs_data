import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xhs_research.cli import main
from xhs_research.config import Config
from xhs_research.errors import SetupError
from xhs_research.export import export_data, sheets
from xhs_research.promotion import (CONFIRMED, NO_SIGNAL, SUSPECTED, UNCERTAIN,
                                    VERSION, classify_all, screen)
from xhs_research.promotion_review import review_export, review_import, review_menu
from xhs_research.runtime import process_lock
from xhs_research.store import Store


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name))
        self.store = Store(self.config.state_dir)
        self.store.register_keywords(self.config)
        job = self.store.next_keyword(self.config)
        self.store.add_search_page(job, [{'id': n, 'token': 'SECRET', 'source': 'pc_search'}
                                        for n in ('ad', 'normal', 'empty')], False, 1, 'test')
        for note_id, text in [('ad', '本次品牌合作，购买链接请私信。'),
                              ('normal', '我家用某品牌奶粉，觉得很好，也有不足。'), ('empty', '')]:
            self.store.save_detail(note_id, {'title': '', 'text': text, 'tags': []}, 2)
            self.store.save_comments_page(note_id, '', '', [{'id': note_id+'c', 'content': '评论'}], False, 3, self.config)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def annotation(self, note_id='ad'):
        return dict(self.store.db.execute('SELECT * FROM promotion_annotations WHERE note_id=?', (note_id,)).fetchone())

    def review_file(self, all_notes=False):
        return review_export(self.store, self.config, 10, all_notes)

    def edit_review(self, path, labels, notes=None):
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            columns, rows = reader.fieldnames, list(reader)
        for row in rows:
            if row['note_id'] in labels:
                row['human_review_label'] = labels[row['note_id']]
                row['human_review_note'] = (notes or {}).get(row['note_id'], '人工阅读全文后判断')
        self.write_review(path, columns, rows)
        return columns, rows

    def write_review(self, path, columns, rows):
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    def test_rules_evidence_and_no_signal_not_authenticity(self):
        for text in ('品牌合作', '品牌赠送进行测评', '专属优惠码', '点击链接购买', '购买请私信', '招募代理'):
            label, evidence = screen({'title': text, 'text': '', 'tags': []})
            self.assertEqual(label, SUSPECTED)
            self.assertTrue(evidence)
            for item in evidence:
                self.assertEqual(text[item['start']:item['end']], item['match'])
                self.assertEqual(item['field'], 'title')
        self.assertEqual(self.annotation('normal')['promotion_label'], NO_SIGNAL)
        self.assertEqual(self.annotation('empty')['promotion_label'], UNCERTAIN)
        # A negative or quoted statement remains only a review prompt, never confirmed.
        self.assertEqual(screen({'title': '没有品牌合作，不要相信“优惠码”套路', 'text': '', 'tags': []})[0], SUSPECTED)

    def test_auto_screen_does_not_exclude_and_skips_same_version(self):
        self.assertEqual(classify_all(self.store, 4), {'classified': 0, 'unchanged': 3, 'pending': 0})
        table = sheets(self.store)['notes']
        self.assertTrue(all(row[table[0].index('exclude_confirmed_promotion')] == 0 for row in table[1:]))
        self.assertEqual(self.annotation()['classified_at'], 2)

    def test_review_default_all_and_round_trip(self):
        path = self.review_file()
        columns, rows = self.edit_review(path, {'ad': CONFIRMED}, {'ad': '合作声明\n人工已确认，保留原帖'})
        self.assertEqual([row['note_id'] for row in rows], ['ad'])
        self.assertEqual(review_import(self.store, path, 11), 1)
        self.assertEqual(review_import(self.store, path, 12), 0)
        self.assertEqual(self.annotation()['human_review_note'], '合作声明\n人工已确认，保留原帖')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM promotion_review_history').fetchone()[0], 1)
        with self.review_file().open(encoding='utf-8-sig') as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 0)
        with self.review_file(True).open(encoding='utf-8-sig') as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 3)

    def test_batch_errors_roll_back_all_rows(self):
        for error in ('label', 'duplicate', 'unknown', 'hash'):
            with self.subTest(error=error):
                path = self.review_file(True)
                columns, rows = self.edit_review(path, {'ad': CONFIRMED, 'normal': '普通经验分享'})
                if error == 'label':
                    rows[1]['human_review_label'] = '不存在的标签'
                elif error == 'duplicate':
                    rows.append(dict(rows[0]))
                elif error == 'unknown':
                    rows[1]['note_id'] = 'unknown'
                else:
                    rows[1]['input_hash'] = 'bad'
                self.write_review(path, columns, rows)
                with self.assertRaisesRegex(SetupError, '第'):
                    review_import(self.store, path, 11)
                self.assertIsNone(self.annotation()['human_review_label'])
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM promotion_review_history').fetchone()[0], 0)

    def test_review_formula_escape_round_trip_does_not_change_note(self):
        path = self.review_file()
        self.edit_review(path, {'ad': CONFIRMED}, {'ad': '=人工备注'})
        review_import(self.store, path, 11)
        exported = review_export(self.store, self.config, 12, True)
        self.assertEqual(review_import(self.store, exported, 13), 0)
        self.assertEqual(self.annotation()['human_review_note'], '=人工备注')

    def test_changed_text_rejects_old_review_and_preserves_human_history(self):
        path = self.review_file()
        self.edit_review(path, {'ad': CONFIRMED})
        review_import(self.store, path, 11)
        self.store.save_detail('ad', {'title': 'changed', 'text': '普通日记', 'tags': []}, 12)
        with self.assertRaises(SetupError):
            review_import(self.store, path, 13)
        self.assertEqual(self.annotation()['human_review_label'], CONFIRMED)
        table = sheets(self.store)['notes']
        row = next(row for row in table[1:] if row[0] == 'ad')
        self.assertEqual(row[table[0].index('exclude_confirmed_promotion')], 0)
        self.assertEqual(row[table[0].index('human_review_current')], 0)

    def test_blank_review_does_not_clear_and_rule_upgrade_preserves_review(self):
        path = self.review_file()
        self.edit_review(path, {'ad': CONFIRMED})
        review_import(self.store, path, 11)
        self.edit_review(path, {'ad': ''})
        self.assertEqual(review_import(self.store, path, 12), 0)
        with patch('xhs_research.promotion.VERSION', 'test-v2'):
            self.assertEqual(classify_all(self.store, 13)['classified'], 3)
        self.assertEqual(self.annotation()['human_review_label'], CONFIRMED)
        self.assertEqual(self.annotation()['classification_version'], 'test-v2')

    def test_filter_excludes_relations_only_after_confirmation_and_can_restore(self):
        initial = export_data(self.store, self.config, 20)
        initial_manifest = json.loads((initial / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(initial_manifest['promotion_filter']['excluded_note_count'], 0)
        path = self.review_file()
        self.edit_review(path, {'ad': CONFIRMED})
        review_import(self.store, path, 21)
        output = export_data(self.store, self.config, 22)
        manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['counts']['notes'], 3)
        self.assertEqual(manifest['promotion_filter']['filtered_counts'], {'notes': 2, 'comments': 2, 'sources': 2})
        self.assertEqual(manifest['promotion_filter']['review_coverage'], 1/3)
        for name in ('notes', 'comments', 'sources'):
            with (output / 'without_confirmed_promotion' / (name+'.csv')).open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertNotIn('ad', {row['note_id'] for row in rows})
        self.assertEqual(self.store.complete_count(), 3)
        self.edit_review(path, {'ad': '普通经验分享'})
        review_import(self.store, path, 23)
        output = export_data(self.store, self.config, 24)
        manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['promotion_filter']['excluded_note_count'], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM promotion_review_history').fetchone()[0], 2)

    def test_classification_failure_keeps_saved_content_and_retry_works(self):
        with patch('xhs_research.promotion.screen', side_effect=ValueError('bad')):
            self.store.save_detail('ad', {'title': 'new', 'text': '新正文', 'tags': []}, 30)
        self.assertEqual(self.annotation()['classification_status'], 'pending')
        self.assertIn('新正文', self.store.db.execute("SELECT detail FROM notes WHERE note_id='ad'").fetchone()[0])
        self.assertEqual(classify_all(self.store, 31)['classified'], 1)

    def test_schema_two_upgrade_is_backed_up_and_keeps_identity(self):
        self.store.db.execute("UPDATE comments SET commenter_user_id='00123',commenter_ip_location='上海'")
        self.store.db.execute('DROP TABLE promotion_review_history')
        self.store.db.execute('DROP TABLE promotion_annotations')
        self.store.set('schema_version', 2)
        self.store.close()
        self.store = Store(self.config.state_dir)
        self.assertEqual(self.store.get('schema_version'), 3)
        self.assertEqual(self.store.db.execute('SELECT commenter_user_id FROM comments LIMIT 1').fetchone()[0], '00123')
        backups = list(self.config.state_dir.glob('research.pre-promotion-*.sqlite3'))
        self.assertEqual(len(backups), 1)
        db = sqlite3.connect(str(backups[0]))
        try:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '2')
        finally:
            db.close()
        self.assertEqual(classify_all(self.store, 30)['classified'], 3)

    def test_cli_menu_cancellation_and_locked_classification(self):
        path = self.config.root / 'config.json'
        path.write_text('{}', encoding='utf-8')
        with patch('builtins.input', return_value='0'):
            self.assertEqual(review_menu(path), 0)
        with process_lock(self.config.state_dir):
            self.assertEqual(main(['--config', str(path), 'classify']), 2)
        self.assertEqual(main(['--config', str(path), 'classify']), 0)
        self.assertEqual(main(['--config', str(path), 'review-export', '--all']), 0)

