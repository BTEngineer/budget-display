from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from budget_display import BudgetValidationError
from budget_display.receipt_store import ReceiptStore


class ReceiptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = ReceiptStore(Path(self.temporary_directory.name), maximum_bytes=32)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_valid_png_is_content_addressed_and_deduplicated(self) -> None:
        content = b"\x89PNG\r\n\x1a\nreceipt"
        first = self.store.store(
            content=content, content_type="image/png", original_filename="../../bad?.png"
        )
        second = self.store.store(
            content=content, content_type="image/png", original_filename="again.png"
        )
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.relative_path, second.relative_path)
        self.assertNotIn("..", first.original_filename)
        self.assertEqual(len(list(Path(self.temporary_directory.name).glob("*.png"))), 1)

        self.store.delete(first.relative_path)
        self.assertEqual(len(list(Path(self.temporary_directory.name).glob("*.png"))), 0)
        self.store.delete(first.relative_path)

    def test_delete_rejects_paths_outside_the_receipt_namespace(self) -> None:
        with self.assertRaises(BudgetValidationError):
            self.store.delete("../budget.db")

    def test_signature_mismatch_and_oversize_fail_closed(self) -> None:
        with self.assertRaisesRegex(BudgetValidationError, "does not match"):
            self.store.store(
                content=b"not an image", content_type="image/jpeg", original_filename="x.jpg"
            )
        with self.assertRaisesRegex(BudgetValidationError, "limit"):
            self.store.store(
                content=b"%PDF-" + b"x" * 40,
                content_type="application/pdf",
                original_filename="x.pdf",
            )


if __name__ == "__main__":
    unittest.main()
