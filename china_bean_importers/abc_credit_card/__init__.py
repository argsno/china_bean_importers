from dateutil.parser import parse
from beangulp import Importer as BaseImporter
from beancount.core import data, amount, flags
from beancount.core.number import D

import re
import sys

from china_bean_importers.common import *

DATE_6_RE = re.compile(r"^\d{6}$")
STMT_CYCLE_RE = re.compile(r"(\d{4}/\d{2}/\d{2})-(\d{4}/\d{2}/\d{2})")


def parse_yymmdd(s, century="20"):
    yy, mm, dd = s[:2], s[2:4], s[4:6]
    return parse(f"{century}{yy}-{mm}-{dd}").date()


class Importer(BaseImporter):
    def __init__(self, config) -> None:
        self.config = config
        self.FLAG = flags.FLAG_OKAY

    def identify(self, filepath):
        if not filepath.upper().endswith(".PDF"):
            return False
        try:
            import fitz
            doc = fitz.open(filepath)
            text = doc[0].get_text("text")
            if "农业银行信用卡对账单" in text or (
                "农业银行" in filepath and "信用卡" in filepath
            ):
                self.doc = doc
                self.full_text = text
                return True
        except BaseException:
            pass
        return False

    def account(self, filepath):
        return "abc_credit_card"

    def date(self, filepath):
        if m := STMT_CYCLE_RE.search(self.full_text):
            return parse(m.group(1)).date()
        return None

    def extract(self, filepath, existing=None):
        entries = []
        section = None

        all_rows = []
        for page in self.doc:
            for tbl in page.find_tables().tables:
                all_rows.extend(tbl.extract())

        for lineno, row in enumerate(all_rows):
            if not row or not row[0]:
                continue

            if "●" in row[0]:
                marker = row[0].strip()
                if "还款" in marker:
                    section = "repayment"
                elif "消费" in marker:
                    section = "expense"
                elif "退货" in marker:
                    section = "refund"
                continue

            if len(row) != 6:
                continue
            if not row[0] or not DATE_6_RE.match(row[0].strip()):
                continue
            if not row[1] or not row[5]:
                continue

            trans_date_str = row[0].strip()
            post_date_str = row[1].strip()
            card_number = row[2].strip() if row[2] else ""
            description = (row[3] or "").replace("\n", "").strip()
            settle_str = (row[5] or "").strip()

            if "/" not in settle_str:
                continue

            amt_str, currency = settle_str.rsplit("/", 1)
            try:
                trans_date = parse_yymmdd(trans_date_str)
                post_date = parse_yymmdd(post_date_str)
            except Exception:
                continue

            units = amount.Amount(D(amt_str), currency)

            metadata = data.new_metadata(filepath, lineno)
            metadata["post_date"] = str(post_date)
            tags = set()

            card_tail = re.sub(r"[^\d]", "", card_number)
            if card_tail:
                account1 = find_account_by_card_number(self.config, card_tail)
                if not account1:
                    account1 = self.config.get("unknown_expense_account", "Expenses:Misc")
                    my_warn(f"Unknown card number {card_tail}", lineno, row)
            else:
                account1 = self.config.get("unknown_expense_account", "Expenses:Misc")

            if section == "repayment":
                tags.add("repayment")
            elif section == "refund" or "退货" in description or "退款" in description:
                tags.add("refund")

            payee = None
            narration = description
            if "，" in description:
                parts = description.split("，", 1)
                prefix = parts[0]
                payee = parts[1]
                if " " in prefix:
                    narration = prefix.split(" ", 1)[0]
                else:
                    narration = prefix
            elif " " in description:
                narration = description

            if in_blacklist(self.config, description):
                print(
                    f"Item skipped due to blacklist: {trans_date} {description} [{units}]",
                    file=sys.stderr,
                )
                continue

            account2 = None
            new_account, new_meta, new_tags = match_destination_and_metadata(
                self.config, description, payee
            )
            if new_account:
                account2 = new_account
            metadata.update(new_meta)
            tags = tags.union(new_tags)
            if account2 is None:
                account2 = unknown_account(self.config, units.number < 0)

            txn = data.Transaction(
                meta=metadata,
                date=trans_date,
                flag=self.FLAG,
                payee=payee,
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
