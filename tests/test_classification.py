"""Meaningful regressions for classification inherited from the legacy tools."""

import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from fileatlas.classification import classify_file
from fileatlas import metadata_media as media
from fileatlas import classification


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.timestamp = datetime(2024, 9, 15, 12).timestamp()
        timestamp_patch = patch(
            "fileatlas.classification._filesystem_timestamp",
            return_value=(self.timestamp, "WINDOWS_CREATION_TIME"),
        )
        timestamp_patch.start()
        self.addCleanup(timestamp_patch.stop)
        self.reader = Mock()
        self.reader.read.return_value = {}
        reader_patch = patch(
            "fileatlas.classification._metadata_reader", return_value=self.reader
        )
        reader_patch.start()
        self.addCleanup(reader_patch.stop)

    def file(self, name):
        path = self.root / name
        path.write_bytes(b"untouched source content")
        return path

    def test_camera_filename_uses_earliest_date_and_year_first_layout(self):
        source = self.file("IMG_20200102_030405.jpg")
        self.reader.read.return_value = {
            "EXIF:Make": "SONY",
            "EXIF:Model": "ILCE-6700",
            "EXIF:DateTimeOriginal": "2022:05:06 07:08:09",
        }
        result = classify_file(source)
        self.assertEqual(result, {
            "category": "picture",
            "relative_path": (
                "picture_classify/2020/2020-01/SONY_ILCE-6700/"
                "IMG_20200102_030405.jpg"
            ),
            "date_source": "FILENAME_DATE",
        })
        self.assertEqual(source.read_bytes(), b"untouched source content")
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_earliest_internal_media_date_is_used(self):
        source = self.file("holiday.mp4")
        self.reader.read.return_value = {
            "QuickTime:CreateDate": "2021:06:07 00:00:00",
            "QuickTime:MediaCreateDate": "2020:12:31 23:59:59",
        }
        result = classify_file(source)
        self.assertEqual(result["category"], "video")
        self.assertIn("video_classify/2020/2020-12/", result["relative_path"])
        self.assertEqual(result["date_source"], "METADATA:MediaCreateDate")

    def test_pdf_is_document_even_if_pillow_registers_pdf(self):
        source = self.file("report.PDF")
        result = classify_file(source)
        self.assertEqual(media.media_type_from_name(source.name), None)
        self.assertEqual(result, {
            "category": "document",
            "relative_path": "document_classify/PDF/2024-09/report.PDF",
            "date_source": "WINDOWS_CREATION_TIME",
        })
        self.reader.read.assert_not_called()

    def test_structured_data_requires_explicit_option(self):
        source = self.file("settings.json")
        self.assertIsNone(classify_file(source))
        self.assertEqual(
            classify_file(source, include_structured=True)["relative_path"],
            "document_classify/Structured_Data/2024-09/settings.json",
        )

    def test_metadata_keys_with_groups_find_device_fields(self):
        self.assertEqual(
            media.device_folder_from_metadata({
                "IFD0:Make": "SONY", "MakerNotes:SonyModelID": "ILCE-6700"
            })[0],
            "SONY_ILCE-6700",
        )
        self.assertEqual(
            media.meta_value({"FFProbe:FormatTag:make": "Apple"}, "Make"),
            "Apple",
        )

    def test_archive_and_unknown_files_stay_unclassified(self):
        self.assertIsNone(classify_file(self.file("backup.zip")))
        self.assertIsNone(classify_file(self.file("unrecognized.fatl")))
        self.reader.read.assert_not_called()

    def test_red_r3d_video_is_not_misidentified_as_still_image(self):
        self.assertEqual(media.media_type_from_name("recording.R3D"), "video")
        self.assertEqual(classify_file(self.file("recording.R3D"))["category"], "video")

    def test_typescript_ts_stays_in_place_without_metadata_probe(self):
        source = self.file("standalone.ts")
        source.write_text("export const answer = 42;\n", encoding="utf-8")
        self.assertIsNone(classify_file(source))
        self.reader.read.assert_not_called()

    def test_mpeg_ts_sync_packets_confirm_video_before_classification(self):
        for name, offsets in (
            ("transport.ts", (0, 188, 376)),
            ("transport-m2ts.ts", (4, 196, 388)),
        ):
            with self.subTest(name=name):
                source = self.file(name)
                content = bytearray(400)
                for index in offsets:
                    content[index] = 0x47
                source.write_bytes(content)
                self.assertEqual(classify_file(source)["category"], "video")

    def test_filename_date_rejects_invalid_calendar_dates(self):
        self.assertEqual(
            media.extract_datetime_from_filename("VID_20200229_235959.mp4"),
            datetime(2020, 2, 29, 23, 59, 59),
        )
        self.assertIsNone(
            media.extract_datetime_from_filename("IMG_20210229_120000.jpg")
        )

    def test_same_metadata_produces_same_proposed_path(self):
        source = self.file("voice.wav")
        first = classify_file(source)
        self.assertEqual(first, classify_file(source))
        relative = Path(first["relative_path"])
        self.assertFalse(relative.is_absolute())
        self.assertNotIn("..", relative.parts)


class MetadataReaderTests(unittest.TestCase):
    def test_portable_app_discovers_tools_beside_executable_and_checks_once(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "FileAtlas.exe"
            with (
                patch.object(classification, "_readers", threading.local()),
                patch.object(classification.sys, "frozen", True, create=True),
                patch.object(classification.sys, "executable", str(executable)),
                patch.object(classification.media, "MetadataReader") as reader_type,
            ):
                first = classification._metadata_reader()
                second = classification._metadata_reader()
                self.assertIs(first, second)
                reader_type.assert_called_once_with(executable.resolve().parent)
                reader_type.return_value.__enter__.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
