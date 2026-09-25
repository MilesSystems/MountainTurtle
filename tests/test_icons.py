from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def icns_entries(path):
    data = path.read_bytes()
    if data[:4] != b"icns":
        raise ValueError(f"{path} is not an ICNS file")
    declared = int.from_bytes(data[4:8], "big")
    if declared != len(data):
        raise ValueError(f"{path} has an invalid ICNS length")
    entries = []
    offset = 8
    while offset < len(data):
        kind = data[offset:offset + 4].decode("ascii")
        length = int.from_bytes(data[offset + 4:offset + 8], "big")
        if length < 8 or offset + length > len(data):
            raise ValueError(f"{path} has an invalid {kind} entry")
        entries.append(kind)
        offset += length
    return entries


class IconTests(unittest.TestCase):
    def test_icons_omit_corrupted_1x_16_slot_but_keep_menu_scale_entries(self):
        icons = [
            ROOT / "Resources/AppIcon.icns",
            ROOT / "Resources/TurtleDocument.icns",
            ROOT / "Resources/S3Drive.icns",
            ROOT / "Resources/SFTPDrive.icns",
            ROOT / "Resources/icon-overlay-assets/VolumeIcon.icns",
            ROOT / "Resources/icon-overlay-assets-sftp/VolumeIcon.icns",
        ]
        for icon in icons:
            with self.subTest(icon=icon.name):
                entries = icns_entries(icon)
                self.assertNotIn("icp4", entries)
                self.assertIn("ic11", entries)
                self.assertIn("icp5", entries)


if __name__ == "__main__":
    unittest.main()
