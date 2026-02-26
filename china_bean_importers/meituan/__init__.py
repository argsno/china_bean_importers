from dateutil.parser import parse
from beancount.core import data, amount
from beancount.core.number import D
import csv
import re

from china_bean_importers.common import (
    find_account_by_card_number,
    match_destination_and_metadata,
    unknown_account,
    my_warn,
)
from china_bean_importers.importer import CsvImporter

# Matches last 4-digit number at end of payment method string.
# Handles both "中国农业银行信用卡(5342)" and "美团联名卡(中国邮政储蓄9512)".
_card_tail_re = re.compile(r"(\d{4})\D*$")


def _extract_card_tail(method: str):
    m = _card_tail_re.search(method)
    return m.group(1) if m else None


class Importer(CsvImporter):
    def __init__(self, config) -> None:
        super().__init__(config)
        self.match_keywords = ["美团交易账单明细", "【美团交易账单明细列表】"]
        self.file_account_name = "meituan"

    def parse_metadata(self, filepath):
        if m := re.search(r"起始时间：\[([0-9-]+)\]", self.full_content):
            self.start = parse(m.group(1))
        if m := re.search(r"终止时间：\[([0-9-]+)\]", self.full_content):
            self.end = parse(m.group(1))

    def extract(self, filepath, existing=None):
        entries = []
        begin = False

        for lineno, row in enumerate(csv.reader(self.content)):
            row = [col.strip() for col in row]
            #    0              1              2        3       4      5       6       7       8       9       10
            # 交易创建时间, 交易成功时间, 交易类型, 订单标题, 收/支, 支付方式, 订单金额, 实付金额, 交易单号, 商家单号, 备注

            if len(row) < 9:
                continue

            if row[0] == "交易创建时间" and row[2] == "交易类型":
                begin = True
                continue

            if not begin:
                continue

            # Pad row to 11 fields
            row = row + ["/"] * max(0, 11 - len(row))
            (
                created_time,
                success_time,
                tx_type,
                narration,
                direction,
                method,
                _order_amount,
                actual_amount,
                serial,
                merchant_serial,
                note,
            ) = row[:11]

            # 不计收支 rows have no clear direction, skip
            if direction not in ("支出", "收入"):
                continue

            expense = direction == "支出"

            # Use success time when available
            time_str = success_time if success_time and success_time != "/" else created_time
            try:
                time = parse(time_str)
            except Exception:
                my_warn(f"Cannot parse time: {time_str!r}", lineno, row)
                continue

            # Parse amount (strip ¥ prefix)
            try:
                units = amount.Amount(D(actual_amount.lstrip("¥").strip()), "CNY")
            except Exception:
                my_warn(f"Cannot parse amount: {actual_amount!r}", lineno, row)
                continue

            if expense:
                units = -units

            tags = set()

            # Refund detection
            if tx_type == "退款" or "退款" in tx_type:
                tags.add("refund")

            # Metadata
            metadata: dict = data.new_metadata(filepath, lineno)
            metadata["payment_method"] = "美团"
            metadata["meituan_payment"] = method
            metadata["time"] = time.time().isoformat()
            metadata["imported_category"] = tx_type
            metadata["serial"] = serial.strip()
            if merchant_serial and merchant_serial != "/":
                metadata["merchant_serial"] = merchant_serial.strip()
            if note and note != "/":
                metadata["note"] = note

            # Append note to narration when present
            if note and note not in ("/", ""):
                narration = f"{narration}（{note}）"

            # Source account: card tail → wechat/alipay fallback → unknown
            account1 = None
            if tail := _extract_card_tail(method):
                account1 = find_account_by_card_number(self.config, tail)
            if account1 is None:
                importers_cfg = self.config.get("importers", {})
                if "微信" in method:
                    account1 = importers_cfg.get("wechat", {}).get("account")
                elif "支付宝" in method:
                    account1 = importers_cfg.get("alipay", {}).get("account")
            if account1 is None:
                account1 = unknown_account(self.config, expense)
                tags.add("confirmation-needed")
                my_warn(f"Unknown payment method: {method!r}", lineno, row)

            # Destination account via detail_mappings
            new_account, new_meta, new_tags = match_destination_and_metadata(
                self.config, narration, None
            )
            account2 = new_account or unknown_account(self.config, expense)
            metadata.update(new_meta)
            tags = tags.union(new_tags)

            txn = data.Transaction(
                meta=metadata,
                date=time.date(),
                flag=self.FLAG,
                payee=None,
                narration=narration,
                tags=tags,
                links=data.EMPTY_SET,
                postings=[
                    data.Posting(
                        account=account1,
                        units=units,
                        cost=None,
                        price=None,
                        flag=None,
                        meta=None,
                    ),
                    data.Posting(
                        account=account2,
                        units=None,
                        cost=None,
                        price=None,
                        flag=None,
                        meta=None,
                    ),
                ],
            )
            entries.append(txn)

        return entries
