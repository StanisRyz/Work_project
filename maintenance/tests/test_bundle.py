import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from accounts.models import Department
from acts.models import Act, ActAttachment, ActCorrectiveAction, ActRootAnalysis
from maintenance import database_transfer as dt
from references.models import ActStatus, DefectType, Operation, TaskStatus
from tasks.models import Task


class PathSafetyTests(SimpleTestCase):
    def test_normalize_rejects_absolute_and_traversal_paths(self):
        for unsafe in (
            '/etc/passwd',
            'C:/Windows/system32',
            '../outside.txt',
            'acts/../../outside.txt',
            '',
            '   ',
        ):
            with self.subTest(path=unsafe):
                with self.assertRaises(dt.UnsafePathError):
                    dt.normalize_relative_path(unsafe)

    def test_normalize_accepts_and_canonicalises_safe_paths(self):
        self.assertEqual(
            dt.normalize_relative_path('acts\\attachments\\3\\file.pdf'),
            'acts/attachments/3/file.pdf',
        )
        self.assertEqual(dt.normalize_relative_path('./a/b.txt'), 'a/b.txt')

    def test_resolve_inside_refuses_to_escape_the_root(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.assertTrue(str(dt.resolve_inside(root, 'a/b.txt')).startswith(str(root.resolve())))
        with self.assertRaises(dt.UnsafePathError):
            dt.resolve_inside(root, '../escape.txt')


class BundleFixtureMixin:
    """Creates a small but representative dataset plus a temporary MEDIA_ROOT."""

    def setUpBundleData(self):
        self.media_root = Path(tempfile.mkdtemp(prefix='media-'))
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        self.output_parent = Path(tempfile.mkdtemp(prefix='bundles-'))
        self.addCleanup(shutil.rmtree, self.output_parent, ignore_errors=True)

        self.status, _ = ActStatus.objects.get_or_create(
            code='CREATED_OTK', defaults={'name': 'Создан ОТК'}
        )
        self.operation = Operation.objects.create(code='BUNDLE_OP', name='Операция')
        self.defect_type = DefectType.objects.create(code='BUNDLE_DEFECT', name='Дефект')
        self.user = User.objects.create_user(username='bundle_user', password='demo12345')
        self.act = Act.objects.create(
            created_by=self.user,
            party_number='P-001',
            nomenclature='Катушка',
            operation=self.operation,
            defect_type=self.defect_type,
            status=self.status,
            description='Описание',
        )

    def add_attachment(self, relative_path='acts/attachments/1/sample.txt', content=b'demo'):
        target = self.media_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return ActAttachment.objects.create(
            act=self.act,
            uploaded_by=self.user,
            file=relative_path,
            original_name='sample.txt',
            file_size=len(content),
        )

    def bundle_path(self, name):
        return self.output_parent / name


@mock.patch.object(connection, 'vendor', 'sqlite')
class ExportTests(BundleFixtureMixin, TestCase):
    def setUp(self):
        self.setUpBundleData()

    def test_manifest_and_data_hash_are_deterministic(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            first = dt.export_bundle(self.bundle_path('first'))
            second = dt.export_bundle(self.bundle_path('second'))

        self.assertEqual(first['data_file']['sha256'], second['data_file']['sha256'])
        self.assertEqual(first['models'], second['models'])
        first_data = (self.bundle_path('first') / dt.DATA_NAME).read_text(encoding='utf-8')
        second_data = (self.bundle_path('second') / dt.DATA_NAME).read_text(encoding='utf-8')
        self.assertEqual(first_data, second_data)

    def test_bundle_contains_the_documented_structure(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            self.add_attachment()
            manifest = dt.export_bundle(self.bundle_path('structured'))

        bundle = self.bundle_path('structured')
        self.assertTrue((bundle / dt.MANIFEST_NAME).is_file())
        self.assertTrue((bundle / dt.DATA_NAME).is_file())
        self.assertTrue((bundle / dt.MEDIA_DIR_NAME).is_dir())
        self.assertEqual(manifest['bundle_format_version'], dt.BUNDLE_FORMAT_VERSION)
        self.assertEqual(manifest['source_vendor'], 'sqlite')
        self.assertEqual(sorted(manifest['models']), sorted(dt.TRANSFERABLE_MODELS))
        self.assertEqual(manifest['excluded_models'], list(dt.EXCLUDED_MODELS))
        self.assertEqual(len(manifest['media_files']), 1)
        self.assertEqual(manifest['models']['acts.Act']['count'], 1)
        self.assertEqual(manifest['models']['acts.Act']['max_pk'], self.act.pk)

    def test_export_refuses_a_non_empty_output_directory(self):
        destination = self.bundle_path('occupied')
        destination.mkdir(parents=True)
        (destination / 'leftover.txt').write_text('x', encoding='utf-8')

        with override_settings(MEDIA_ROOT=self.media_root):
            with self.assertRaisesMessage(dt.TransferError, 'не пуст'):
                dt.export_bundle(destination)

    def test_export_refuses_a_missing_attachment_file_by_default(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            attachment = self.add_attachment()
            (self.media_root / attachment.file.name).unlink()

            with self.assertRaisesMessage(dt.TransferError, 'отсутствует'):
                dt.export_bundle(self.bundle_path('missing'))

        self.assertFalse(self.bundle_path('missing').exists())

    def test_allow_missing_media_records_the_gap_in_the_manifest(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            attachment = self.add_attachment()
            (self.media_root / attachment.file.name).unlink()
            manifest = dt.export_bundle(
                self.bundle_path('tolerated'), allow_missing_media=True
            )

        self.assertEqual(manifest['media_files'], [])
        self.assertEqual(len(manifest['missing_media']), 1)
        self.assertEqual(manifest['missing_media'][0]['attachment_id'], attachment.pk)
        self.assertFalse(manifest['complete'])
        self.assertTrue(any('неполный' in warning for warning in manifest['warnings']))

    def test_export_refuses_an_attachment_path_outside_media_root(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            ActAttachment.objects.create(
                act=self.act,
                uploaded_by=self.user,
                file='../../escape.txt',
                original_name='escape.txt',
                file_size=1,
            )
            with self.assertRaises(dt.UnsafePathError):
                dt.export_bundle(self.bundle_path('traversal'))

    def test_a_failed_export_leaves_no_partial_bundle(self):
        destination = self.bundle_path('aborted')
        with override_settings(MEDIA_ROOT=self.media_root):
            attachment = self.add_attachment()
            (self.media_root / attachment.file.name).unlink()
            with self.assertRaises(dt.TransferError):
                dt.export_bundle(destination)

        self.assertFalse(destination.exists())


class ExportBackendGuardTests(BundleFixtureMixin, TestCase):
    def setUp(self):
        self.setUpBundleData()

    def test_export_is_refused_on_a_non_sqlite_backend(self):
        with mock.patch.object(connection, 'vendor', 'postgresql'):
            with override_settings(MEDIA_ROOT=self.media_root):
                with self.assertRaisesMessage(dt.TransferError, 'только на SQLite'):
                    dt.export_bundle(self.bundle_path('wrong-backend'))


@mock.patch.object(connection, 'vendor', 'sqlite')
class BundleValidationTests(BundleFixtureMixin, TestCase):
    def setUp(self):
        self.setUpBundleData()

    def _export(self, name='valid'):
        with override_settings(MEDIA_ROOT=self.media_root):
            self.add_attachment()
            dt.export_bundle(self.bundle_path(name))
        return self.bundle_path(name)

    def _rewrite_manifest(self, bundle, mutate):
        manifest = json.loads((bundle / dt.MANIFEST_NAME).read_text(encoding='utf-8'))
        mutate(manifest)
        (bundle / dt.MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False), encoding='utf-8'
        )

    def test_a_valid_bundle_passes_validation(self):
        bundle = self._export()
        result = dt.validate_bundle(bundle)
        self.assertEqual(result['media_count'], 1)
        self.assertGreater(result['record_count'], 0)

    def test_corrupted_data_file_is_rejected(self):
        bundle = self._export()
        (bundle / dt.DATA_NAME).write_text('[]', encoding='utf-8')

        with self.assertRaisesMessage(dt.TransferError, 'Контрольная сумма'):
            dt.validate_bundle(bundle)

    def test_unparsable_data_file_is_rejected(self):
        bundle = self._export()
        broken = 'not json at all'
        (bundle / dt.DATA_NAME).write_text(broken, encoding='utf-8')
        self._rewrite_manifest(
            bundle, lambda m: m['data_file'].__setitem__('sha256', dt.text_sha256(broken))
        )

        with self.assertRaisesMessage(dt.TransferError, 'повреждён'):
            dt.validate_bundle(bundle)

    def test_unknown_bundle_format_version_is_rejected(self):
        bundle = self._export()
        self._rewrite_manifest(bundle, lambda m: m.update({'bundle_format_version': 99}))

        with self.assertRaisesMessage(dt.TransferError, 'Неподдерживаемая версия'):
            dt.validate_bundle(bundle)

    def test_missing_manifest_is_rejected(self):
        bundle = self._export()
        (bundle / dt.MANIFEST_NAME).unlink()

        with self.assertRaisesMessage(dt.TransferError, 'нет manifest.json'):
            dt.validate_bundle(bundle)

    def test_missing_media_file_is_rejected(self):
        bundle = self._export()
        stored = bundle / dt.MEDIA_DIR_NAME / 'acts/attachments/1/sample.txt'
        stored.unlink()

        with self.assertRaisesMessage(dt.TransferError, 'нет файла вложения'):
            dt.validate_bundle(bundle)

    def test_corrupted_media_file_is_rejected(self):
        bundle = self._export()
        stored = bundle / dt.MEDIA_DIR_NAME / 'acts/attachments/1/sample.txt'
        stored.write_bytes(b'tampered content')

        with self.assertRaises(dt.TransferError) as ctx:
            dt.validate_bundle(bundle)
        self.assertRegex(str(ctx.exception), 'Размер файла|Контрольная сумма файла')

    def test_missing_model_entry_is_rejected(self):
        bundle = self._export()
        self._rewrite_manifest(bundle, lambda m: m['models'].pop('acts.Act'))

        with self.assertRaisesMessage(dt.TransferError, 'нет данных по моделям'):
            dt.validate_bundle(bundle)

    def test_unknown_model_entry_is_rejected(self):
        bundle = self._export()
        self._rewrite_manifest(
            bundle,
            lambda m: m['models'].__setitem__('ghost.Model', {'count': 0, 'max_pk': None, 'hash': ''}),
        )

        with self.assertRaisesMessage(dt.TransferError, 'неизвестные модели'):
            dt.validate_bundle(bundle)

    def test_record_count_mismatch_is_rejected(self):
        bundle = self._export()
        self._rewrite_manifest(bundle, lambda m: m['models']['acts.Act'].__setitem__('count', 99))

        with self.assertRaisesMessage(dt.TransferError, 'manifest заявляет'):
            dt.validate_bundle(bundle)

    def test_media_entry_with_traversal_path_is_rejected(self):
        bundle = self._export()
        self._rewrite_manifest(
            bundle,
            lambda m: m['media_files'].__setitem__(
                0, {'path': '../escape.txt', 'size': 4, 'sha256': 'x' * 64}
            ),
        )

        with self.assertRaises(dt.UnsafePathError):
            dt.validate_bundle(bundle)

    def _rewrite_data(self, bundle, mutate):
        """Rewrite data.json and refresh its manifest checksum.

        Without refreshing the checksum every mutation would be caught by the
        data.json hash instead of by the per-model recomputation under test.
        """
        records = json.loads((bundle / dt.DATA_NAME).read_text(encoding='utf-8'))
        mutate(records)
        text = json.dumps(records, indent=2, sort_keys=True, ensure_ascii=False)
        (bundle / dt.DATA_NAME).write_text(text, encoding='utf-8')
        self._rewrite_manifest(
            bundle,
            lambda m: m['data_file'].update({'sha256': dt.text_sha256(text), 'size': len(text)}),
        )

    def test_validation_recomputes_model_statistics_from_data_file(self):
        bundle = self._export()
        result = dt.validate_bundle(bundle)

        manifest = result['manifest']
        recomputed = result['recomputed_models']
        self.assertEqual(sorted(recomputed), sorted(dt.TRANSFERABLE_MODELS))
        for label in dt.TRANSFERABLE_MODELS:
            self.assertEqual(recomputed[label]['count'], manifest['models'][label]['count'])
            self.assertEqual(recomputed[label]['max_pk'], manifest['models'][label]['max_pk'])
            self.assertEqual(recomputed[label]['hash'], manifest['models'][label]['hash'])
        self.assertEqual(recomputed['acts.Act']['count'], 1)
        self.assertEqual(recomputed['acts.Act']['max_pk'], self.act.pk)

    def test_wrong_record_distribution_between_models_is_rejected(self):
        bundle = self._export()

        def relabel_one_act(records):
            for record in records:
                if record['model'] == 'acts.act':
                    record['model'] = 'acts.actcomment'
                    return
            raise AssertionError('В пакете нет записей acts.Act.')

        self._rewrite_data(bundle, relabel_one_act)

        with self.assertRaises(dt.TransferError) as ctx:
            dt.validate_bundle(bundle)
        self.assertIn('acts.Act', str(ctx.exception))

    def test_record_of_an_unknown_model_is_rejected(self):
        bundle = self._export()
        self._rewrite_data(
            bundle, lambda records: records.append({'model': 'ghost.model', 'pk': 1, 'fields': {}})
        )

        with self.assertRaisesMessage(dt.TransferError, 'неразрешённой модели'):
            dt.validate_bundle(bundle)

    def test_recomputed_hash_mismatch_is_rejected(self):
        bundle = self._export()

        def edit_first_act(records):
            for record in records:
                if record['model'] == 'acts.act':
                    record['fields']['nomenclature'] = 'Подменено'
                    return
            raise AssertionError('В пакете нет записей acts.Act.')

        self._rewrite_data(bundle, edit_first_act)

        with self.assertRaisesMessage(dt.TransferError, 'хеш данных в manifest не совпадает'):
            dt.validate_bundle(bundle)

    def test_wrong_model_order_is_rejected(self):
        bundle = self._export()
        reordered = list(dt.TRANSFERABLE_MODELS)
        reordered[0], reordered[1] = reordered[1], reordered[0]
        self._rewrite_manifest(bundle, lambda m: m.update({'model_order': reordered}))

        with self.assertRaisesMessage(dt.TransferError, 'Порядок моделей'):
            dt.validate_bundle(bundle)

    def test_duplicated_model_in_model_order_is_rejected(self):
        bundle = self._export()
        duplicated = list(dt.TRANSFERABLE_MODELS) + ['acts.Act']
        self._rewrite_manifest(bundle, lambda m: m.update({'model_order': duplicated}))

        with self.assertRaisesMessage(dt.TransferError, 'повторяются модели'):
            dt.validate_bundle(bundle)

    def test_unknown_model_in_model_order_is_rejected(self):
        bundle = self._export()
        extended = list(dt.TRANSFERABLE_MODELS) + ['ghost.Model']
        self._rewrite_manifest(bundle, lambda m: m.update({'model_order': extended}))

        with self.assertRaisesMessage(dt.TransferError, 'неизвестные модели'):
            dt.validate_bundle(bundle)

    def test_non_sqlite_source_vendor_is_rejected(self):
        bundle = self._export()
        self._rewrite_manifest(bundle, lambda m: m.update({'source_vendor': 'postgresql'}))

        with self.assertRaisesMessage(dt.TransferError, 'собран не из SQLite'):
            dt.validate_bundle(bundle)

    def test_pending_migrations_in_the_manifest_are_rejected(self):
        bundle = self._export()
        self._rewrite_manifest(
            bundle, lambda m: m['migration_state'].__setitem__('pending', ['acts.9999_future'])
        )

        with self.assertRaisesMessage(dt.TransferError, 'непринятыми миграциями'):
            dt.validate_bundle(bundle)

    def test_broken_migration_state_block_is_rejected(self):
        bundle = self._export()
        self._rewrite_manifest(bundle, lambda m: m.update({'migration_state': 'нет'}))

        with self.assertRaisesMessage(dt.TransferError, '"migration_state"'):
            dt.validate_bundle(bundle)

    def test_broken_missing_media_block_is_rejected(self):
        bundle = self._export()
        self._rewrite_manifest(
            bundle, lambda m: m.update({'missing_media': [{'path': 'a/b.txt'}]})
        )

        with self.assertRaisesMessage(dt.TransferError, 'attachment_id'):
            dt.validate_bundle(bundle)

    def test_duplicated_media_path_is_rejected(self):
        bundle = self._export()
        self._rewrite_manifest(
            bundle, lambda m: m['media_files'].append(dict(m['media_files'][0]))
        )

        with self.assertRaisesMessage(dt.TransferError, 'повторяется путь'):
            dt.validate_bundle(bundle)

    def test_invalid_media_checksum_format_is_rejected(self):
        bundle = self._export()
        self._rewrite_manifest(
            bundle, lambda m: m['media_files'][0].__setitem__('sha256', 'not-a-digest')
        )

        with self.assertRaisesMessage(dt.TransferError, 'некорректная контрольная сумма'):
            dt.validate_bundle(bundle)


@mock.patch.object(connection, 'vendor', 'sqlite')
class VerificationTests(BundleFixtureMixin, TestCase):
    def setUp(self):
        self.setUpBundleData()

    def _export(self, name='verify'):
        with override_settings(MEDIA_ROOT=self.media_root):
            self.add_attachment()
            dt.export_bundle(self.bundle_path(name))
        return self.bundle_path(name)

    def test_verification_passes_against_the_source_database(self):
        bundle = self._export()
        with override_settings(MEDIA_ROOT=self.media_root):
            report = dt.verify_against_bundle(bundle)

        self.assertTrue(report['ok'], report['differences'])
        self.assertEqual(report['media']['checked'], 1)

    def test_verification_detects_a_row_count_difference(self):
        bundle = self._export()
        Act.objects.create(
            created_by=self.user,
            party_number='P-002',
            nomenclature='Катушка',
            operation=self.operation,
            defect_type=self.defect_type,
            status=self.status,
            description='Ещё один',
        )

        with override_settings(MEDIA_ROOT=self.media_root):
            report = dt.verify_against_bundle(bundle)

        self.assertFalse(report['ok'])
        self.assertFalse(report['models']['acts.Act']['matches'])
        self.assertTrue(any('acts.Act' in item for item in report['differences']))

    def test_verification_detects_changed_field_values(self):
        bundle = self._export()
        Act.objects.filter(pk=self.act.pk).update(nomenclature='Изменено')

        with override_settings(MEDIA_ROOT=self.media_root):
            report = dt.verify_against_bundle(bundle)

        self.assertFalse(report['ok'])
        self.assertTrue(any('хеш данных' in item for item in report['differences']))

    def test_verification_detects_a_missing_media_file(self):
        bundle = self._export()
        (self.media_root / 'acts/attachments/1/sample.txt').unlink()

        with override_settings(MEDIA_ROOT=self.media_root):
            report = dt.verify_against_bundle(bundle)

        self.assertFalse(report['ok'])
        self.assertTrue(any('файл отсутствует' in item for item in report['differences']))

    def test_verification_detects_a_tampered_media_file(self):
        bundle = self._export()
        (self.media_root / 'acts/attachments/1/sample.txt').write_bytes(b'tampered')

        with override_settings(MEDIA_ROOT=self.media_root):
            report = dt.verify_against_bundle(bundle)

        self.assertFalse(report['ok'])
        self.assertTrue(
            any('размер' in item or 'контрольная сумма' in item for item in report['differences'])
        )

    def _create_task(self, root_analysis, source_action):
        department = Department.objects.create(code='VERIFY_DEP', name='Отдел')
        task_status, _ = TaskStatus.objects.get_or_create(
            code='IN_PROGRESS', defaults={'name': 'В работе'}
        )
        return Task.objects.create(
            source_action=source_action,
            act=root_analysis.act,
            root_analysis=root_analysis,
            task_text='Мероприятие',
            department=department,
            due_date=timezone.localdate(),
            created_by=self.user,
            status=task_status,
        )

    def test_invariants_detect_inconsistent_task_links(self):
        # Foreign keys stay valid, but the task points at a root analysis that
        # is not the one its source action belongs to.
        first_root = ActRootAnalysis.objects.create(act=self.act, root_cause='Первая')
        second_root = ActRootAnalysis.objects.create(act=self.act, root_cause='Вторая')
        department = Department.objects.create(code='VERIFY_ACT_DEP', name='Отдел мероприятия')
        action = ActCorrectiveAction.objects.create(
            root_analysis=first_root,
            comment='Мероприятие',
            department=department,
            due_date=timezone.localdate(),
        )
        self._create_task(second_root, action)

        problems = dt.check_relational_invariants()['problems']

        self.assertTrue(any('tasks.Task' in problem for problem in problems), problems)

    def test_invariants_detect_a_lagging_act_number_sequence(self):
        Act.objects.create(
            number='АОК-2030-050',
            created_by=self.user,
            party_number='P-777',
            nomenclature='Катушка',
            operation=self.operation,
            defect_type=self.defect_type,
            status=self.status,
            description='Будущий год',
        )

        problems = dt.check_relational_invariants()['problems']

        self.assertTrue(
            any('ActNumberSequence' in problem and '2030' in problem for problem in problems),
            problems,
        )

    def test_verification_reports_relation_problems(self):
        bundle = self._export()
        Act.objects.create(
            number='АОК-2031-077',
            created_by=self.user,
            party_number='P-778',
            nomenclature='Катушка',
            operation=self.operation,
            defect_type=self.defect_type,
            status=self.status,
            description='Без счётчика',
        )

        with override_settings(MEDIA_ROOT=self.media_root):
            report = dt.verify_against_bundle(bundle)

        self.assertFalse(report['ok'])
        self.assertTrue(
            any('ActNumberSequence' in item for item in report['differences']),
            report['differences'],
        )


@mock.patch.object(connection, 'vendor', 'sqlite')
class SourceMediaRootTests(BundleFixtureMixin, TestCase):
    """`--source-media-root` must really drive where attachments are read from."""

    def setUp(self):
        self.setUpBundleData()
        self.separate_media = Path(tempfile.mkdtemp(prefix='media-copy-'))
        self.addCleanup(shutil.rmtree, self.separate_media, ignore_errors=True)

    def _write_into(self, root, relative_path, content):
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def test_attachments_are_read_from_the_explicit_media_copy(self):
        relative = 'acts/attachments/1/sample.txt'
        # Only the copy holds the file; the default MEDIA_ROOT stays empty.
        self._write_into(self.separate_media, relative, b'copy content')
        ActAttachment.objects.create(
            act=self.act,
            uploaded_by=self.user,
            file=relative,
            original_name='sample.txt',
            file_size=len(b'copy content'),
        )

        with override_settings(MEDIA_ROOT=self.media_root):
            manifest = dt.export_bundle(
                self.bundle_path('from-copy'), source_media_root=self.separate_media
            )

        self.assertEqual(len(manifest['media_files']), 1)
        self.assertFalse(manifest['source_media']['is_default_media_root'])
        self.assertEqual(manifest['source_media']['name'], self.separate_media.name)
        stored = self.bundle_path('from-copy') / dt.MEDIA_DIR_NAME / relative
        self.assertEqual(stored.read_bytes(), b'copy content')

    def test_default_media_root_is_used_when_no_override_is_given(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            self.add_attachment(content=b'default content')
            manifest = dt.export_bundle(self.bundle_path('default-media'))

        self.assertTrue(manifest['source_media']['is_default_media_root'])
        self.assertEqual(manifest['source_media']['name'], self.media_root.name)

    def test_manifest_never_stores_the_absolute_media_path(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            manifest = dt.export_bundle(
                self.bundle_path('safe-manifest'), source_media_root=self.separate_media
            )

        serialized = json.dumps(manifest, ensure_ascii=False)
        self.assertNotIn(str(self.separate_media), serialized)
        self.assertNotIn(str(self.separate_media.parent), serialized)

    def test_a_missing_media_directory_is_refused(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            with self.assertRaisesMessage(dt.TransferError, 'Каталог media не найден'):
                dt.export_bundle(
                    self.bundle_path('no-media'),
                    source_media_root=self.separate_media / 'does-not-exist',
                )

    def test_a_file_used_as_media_root_is_refused(self):
        as_file = self.separate_media / 'not-a-directory.txt'
        as_file.write_text('x', encoding='utf-8')

        with override_settings(MEDIA_ROOT=self.media_root):
            with self.assertRaisesMessage(dt.TransferError, 'не является каталогом'):
                dt.export_bundle(self.bundle_path('file-media'), source_media_root=as_file)


class IncompleteBundleTests(BundleFixtureMixin, TestCase):
    """A bundle with missing_media is incomplete everywhere it is consumed.

    The vendor patches are applied one at a time, never nested: `connection`
    is a proxy whose `vendor` lives on the class, so an inner patch would
    delete the attribute the outer one restored.
    """

    def setUp(self):
        self.setUpBundleData()

    def _export_incomplete(self, name='incomplete'):
        with mock.patch.object(connection, 'vendor', 'sqlite'):
            with override_settings(MEDIA_ROOT=self.media_root):
                attachment = self.add_attachment()
                (self.media_root / attachment.file.name).unlink()
                dt.export_bundle(self.bundle_path(name), allow_missing_media=True)
        return self.bundle_path(name), attachment

    def test_validation_marks_the_bundle_as_incomplete(self):
        bundle, attachment = self._export_incomplete()

        result = dt.validate_bundle(bundle)

        self.assertFalse(result['complete'])
        self.assertEqual(len(result['missing_media']), 1)
        self.assertEqual(result['missing_media'][0]['attachment_id'], attachment.pk)
        self.assertTrue(any('неполный' in warning for warning in result['warnings']))

    def test_a_normal_import_refuses_an_incomplete_bundle(self):
        bundle, attachment = self._export_incomplete()

        with mock.patch.object(connection, 'vendor', 'postgresql'):
            with override_settings(EMAIL_NOTIFICATIONS_ENABLED=False):
                with self.assertRaises(dt.TransferError) as ctx:
                    dt.check_import_preconditions(bundle)

        message = str(ctx.exception)
        self.assertIn('Пакет неполный', message)
        self.assertIn('--accept-missing-media', message)
        self.assertIn(str(attachment.pk), message)

    def test_accept_missing_media_gets_past_the_completeness_check(self):
        bundle, _attachment = self._export_incomplete()

        with mock.patch.object(connection, 'vendor', 'postgresql'):
            with override_settings(EMAIL_NOTIFICATIONS_ENABLED=False):
                with self.assertRaises(dt.TransferError) as ctx:
                    dt.check_import_preconditions(bundle, accept_missing_media=True)

        # The next refusal is the ordinary non-empty-tables one, not completeness.
        self.assertIn('только в пустые переносимые таблицы', str(ctx.exception))

    def test_verification_reports_missing_media_as_a_difference(self):
        bundle, attachment = self._export_incomplete()

        with override_settings(MEDIA_ROOT=self.media_root):
            report = dt.verify_against_bundle(bundle)

        self.assertFalse(report['ok'])
        self.assertFalse(report['complete_transfer'])
        self.assertTrue(
            any('перенос неполный' in item for item in report['differences']),
            report['differences'],
        )
        self.assertEqual(len(report['missing_media']), 1)
        self.assertIn(str(attachment.pk), report['missing_media'][0])

    def test_verification_can_tolerate_missing_media_only_in_the_special_mode(self):
        bundle, _attachment = self._export_incomplete()

        with override_settings(MEDIA_ROOT=self.media_root):
            report = dt.verify_against_bundle(bundle, allow_missing_media=True)

        self.assertTrue(report['ok'], report['differences'])
        self.assertFalse(report['complete_transfer'])
        self.assertTrue(report['missing_media_allowed'])
