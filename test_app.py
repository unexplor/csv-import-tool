import csv
from contextlib import closing
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from app import FIELDS, MAX_BYTES, import_csv, init_db, make_server, preview


SAMPLES = Path(__file__).with_name("samples")


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(FIELDS)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


class ImportTests(unittest.TestCase):
    def test_mixed_file_and_physical_lines(self):
        result = preview((SAMPLES / "mixed.csv").read_bytes())
        self.assertEqual((result["total"], result["valid"], result["invalid"]), (12, 4, 8))
        self.assertEqual(result["rows"][1]["values"]["name"], "礼盒,豪华版")
        multiline = result["rows"][2]
        self.assertEqual((multiline["line"], multiline["end_line"]), (4, 5))
        self.assertEqual(multiline["values"]["name"], "多行商品\n第二行名称")
        self.assertEqual(result["rows"][3]["line"], 6)
        self.assertEqual(result["rows"][-1]["values"]["name"], '他说"你好"')
        for row in result["rows"][3:5]:
            self.assertTrue(any(e["field"] == "code" and "重复" in e["reason"] for e in row["errors"]))

    def test_required_fields_and_numeric_boundaries(self):
        invalid_prices = ["", "-1", "1.234", "NaN", "Infinity", "1e2", "￥1", ".5", "1.", "+1", "１"]
        invalid_stocks = ["", "-1", "1.0", "1e2", "NaN", "+1", "１"]
        for price in invalid_prices:
            with self.subTest(price=price):
                row = preview(csv_bytes([["X", "name", price, "0"]]))["rows"][0]
                self.assertIn("price", [e["field"] for e in row["errors"]])
        for stock in invalid_stocks:
            with self.subTest(stock=stock):
                row = preview(csv_bytes([["X", "name", "0", stock]]))["rows"][0]
                self.assertIn("stock", [e["field"] for e in row["errors"]])
        result = preview(csv_bytes([[" ", "\t", "0", "0"]]))
        self.assertEqual({e["field"] for e in result["rows"][0]["errors"]}, {"code", "name"})
        for price in ["0", "0.00", "1.2", "01.20", "99999999999999999999.99"]:
            self.assertEqual(preview(csv_bytes([["X", "name", price, "000"]]))["valid"], 1)

    def test_duplicate_with_invalid_row_and_trimmed_code(self):
        result = preview(csv_bytes([[" X ", "n", "1", "0"], ["X", "n", "-1", "0"],
                                    ["X", "n", "2", "0"], ["x", "n", "0", "0"]]))
        self.assertEqual(result["valid"], 1)  # Codes are case-sensitive.
        for row in result["rows"][:3]:
            self.assertIn("code", [e["field"] for e in row["errors"]])

    def test_bom_crlf_and_blank_lines(self):
        data = b'\xef\xbb\xbfcode,name,price,stock\r\nA,"one\r\ntwo",0,0\r\n\r\nB,b,1,1\r\n'
        result = preview(data)
        self.assertEqual((result["total"], result["valid"], result["invalid"]), (3, 2, 1))
        self.assertEqual([r["line"] for r in result["rows"]], [2, 4, 5])
        self.assertEqual(result["rows"][0]["values"]["name"], "one\r\ntwo")

    def test_column_errors_do_not_hide_valid_rows(self):
        result = preview(csv_bytes([["A", "n", "1"], ["B", "n", "1", "1", "extra"],
                                    ["C", "n", "1", "1"]]))
        self.assertEqual((result["total"], result["valid"]), (3, 1))
        for row in result["rows"][:2]:
            self.assertIn("CSV", [e["field"] for e in row["errors"]])

    def test_header_encoding_size_and_broken_quotes(self):
        for data in [b"", b"name,code,price,stock\n", b"code,name,price,stock,extra\n", b"\xff",
                     b"x" * (MAX_BYTES + 1)]:
            with self.subTest(data=data[:40]), self.assertRaises(ValueError):
                preview(data)
        self.assertEqual(preview(b"code,name,price,stock\n")["total"], 0)
        result = preview(b'code,name,price,stock\nA,ok,1,1\nB,"unclosed\nname,1,1')
        self.assertEqual((result["valid"], result["invalid"]), (1, 1))
        self.assertEqual(result["rows"][1]["errors"][0]["field"], "CSV")

    def test_insert_update_repeat_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            init_db(path)
            mixed = (SAMPLES / "mixed.csv").read_bytes()
            first = import_csv(path, mixed)
            self.assertEqual(first, {"inserted": 4, "updated": 0, "unchanged": 0,
                                     "skipped": 8, "total_products": 4})
            second = import_csv(path, mixed)
            self.assertEqual((second["inserted"], second["updated"], second["unchanged"]), (0, 0, 4))
            update = (SAMPLES / "update.csv").read_bytes()
            third = import_csv(path, update)
            self.assertEqual((third["inserted"], third["updated"], third["total_products"]), (1, 1, 5))
            self.assertEqual(import_csv(path, update)["unchanged"], 2)
            init_db(path)  # Starting again preserves data.
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("SELECT name,price,stock FROM products WHERE code='A001'").fetchone(),
                                 ("苹果（更新）", "4.20", "20"))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM products WHERE code='DUP'").fetchone()[0], 0)

    def test_exact_numbers_and_sql_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            init_db(path)
            code = "'; DROP TABLE products; --"
            price, stock = "99999999999999999999.99", "999999999999999999999999"
            result = import_csv(path, csv_bytes([[code, '<script>alert("x")</script>', price, stock]]))
            self.assertEqual(result["inserted"], 1)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("SELECT price,stock FROM products").fetchone(), (price, stock))

    def test_transaction_rolls_back_on_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            init_db(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON products WHEN NEW.code='B'
                              BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                import_csv(path, csv_bytes([["A", "n", "1", "1"], ["B", "n", "2", "2"]]))
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)

    def test_real_http_preview_import_and_revalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            server = make_server(Path(directory) / "http.db", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                page = response.read().decode()
                self.assertIn("商品 CSV 导入", page)
                self.assertNotIn("__TOKEN__", page)
                headers = {"X-Import-Token": server.token, "Content-Type": "application/octet-stream"}
                data = csv_bytes([["A", "ok", "1.20", "2"]])
                for endpoint in ("preview", "import", "import"):
                    connection.request("POST", f"/api/{endpoint}", data, headers)
                    response = connection.getresponse()
                    result = json.loads(response.read())
                    self.assertEqual(response.status, 200, result)
                self.assertEqual((result["unchanged"], result["total_products"]), (1, 1))
                connection.request("POST", "/api/import", csv_bytes([["A", "bad", "-1", "2"]]), headers)
                response = connection.getresponse()
                self.assertEqual(json.loads(response.read())["skipped"], 1)
                connection.request("POST", "/api/import", data)
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                connection.request("POST", "/api/import", b"wrong,header\n", headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
