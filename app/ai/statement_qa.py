"""Ask about your statement: answers a customer's questions about their own bank statement.

A question is parsed into an action (total, count, list, largest, smallest, average, which month,
trend, breakdown, balance, account health) and filters (money in or out, category, payment channel,
counterparty, month or date range, amount). The answer is computed exactly from the case's own
transactions and cites the rows it used, so the customer can check it against their statement.

Borrower-safe by construction: it reads transactions, categories and balances only, never scores,
bands, signals or the decision, and it refuses questions about them, about other people, and
requests for investment advice. With a model configured, the model may rephrase the computed
answer, but its draft is discarded if it adds any figure or internal term.
"""
from __future__ import annotations

import calendar
import json
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.ai.explain import INTERNAL_TERMS, NUMBER
from app.ai.gateway import LLMGateway, LLMUnavailable, UsageMeter
from app.ai.schemas import CoachDraft
from app.cashflow.categorise import BUSINESS_CREDIT
from app.domain.formatting import inr
from app.domain.models import Case, Transaction
from app.security.injection import detect

LANGS = ("en", "hi", "id")
MAX_ROWS = 8          # rows listed in an answer
MAX_EVIDENCE = 60     # evidence ids returned with an answer
FULL_MONTH = 0.8      # share of a month's days a statement must cover for month-to-month comparisons

# --------------------------------------------------------------------------- vocabulary

MONTHS = {
    **{name: i for i, name in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
         "november", "december"], 1)},
    **{name: i for i, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)},
    "sept": 9,
    **{name: i for i, name in enumerate(
        ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"], 1)},
    "फरवरी": 2, "सितम्बर": 9, "अक्तूबर": 10, "नवम्बर": 11, "दिसम्बर": 12,
    **{name: i for i, name in enumerate(
        ["januari", "februari", "maret", "april", "mei", "juni", "juli", "agustus", "september", "oktober",
         "november", "desember"], 1)},
}

IN_WORDS = ("receive", "received", "came in", "come in", " get ", " got ", "collected", "credited", "credit", "deposit", "inflow", "earn",
            "income", "sales", "got paid", "मिला", "मिले", "आया", "आए", "जमा", "प्राप्त", "कमाई", "आमदनी", "आय",
            "masuk", "terima", "diterima", "pemasukan", "pendapatan", "setor")
OUT_WORDS = ("paid", "pay ", "pay?", "payment", "spent", "spend", "debit", "expense", "outflow", "sent", "withdr", "went out",
             "खर्च", "भुगतान", "भेजा", "दिया", "निकाला", "keluar", "bayar", "dibayar", "pengeluaran", "tarik", "kirim")
INCOME_WORDS = ("income", "earn", "sales", "business", "कमाई", "आमदनी", "आय", "pendapatan", "pemasukan", "penjualan")
SPEND_WORDS = ("spend", "spent", "spending", "expense", "outflow", "खर्च", "pengeluaran", "belanja")

# (keywords, direction, categories)
CATEGORY_WORDS: tuple[tuple[tuple[str, ...], str, frozenset[str]], ...] = (
    (("rent", "किराया", "sewa"), "out", frozenset({"rent"})),
    (("salary", "salaries", "wage", "staff", "वेतन", "तनख्वाह", "gaji", "upah"), "out", frozenset({"staff_salary"})),
    (("bill", "electricity", "utilit", "water", "broadband", "gas", "बिजली", "बिल", "listrik", "tagihan"), "out",
     frozenset({"utilities"})),
    (("emi", "instalment", "installment", "loan repay", "किश्त", "किस्त", "cicilan", "angsuran"), "out",
     frozenset({"emi_debit"})),
    (("supplier", "stock", "purchase", "vendor", "wholesale", "सप्लायर", "माल", "pemasok", "pembelian"), "out",
     frozenset({"supplier_payment"})),
    (("atm", "withdraw", "निकासी", "निकाला", "tarik tunai"), "out", frozenset({"cash_withdrawal"})),
    (("gst", "tax", "tds", "टैक्स", "आयकर", "pajak"), "out", frozenset({"tax_payment"})),
    (("charge", "fee", "शुल्क", "biaya"), "out", frozenset({"bank_charges", "return_charges"})),
    (("loan disburs", "loan received", "loan credit", "लोन मिला", "pencairan"), "in", frozenset({"loan_disbursal"})),
    (("card", "pos ", "कार्ड", "kartu"), "in", frozenset({"card_settlement"})),
)
# Groups recognised from transaction descriptions rather than categories (the categoriser files most UPI
# debits as supplier and other payments). Keywords that ask for a group -> (direction, description pattern).
TRAVEL_PATTERN = (r"\b(irctc|rail|railway|indigo|air ?india|akasa|spicejet|vistara|airline|airways|uber|ola ?cabs?|ola|"
                  r"rapido|taxi|cab|redbus|bus ticket|makemytrip|goibibo|cleartrip|yatra|oyo|hotel|fastag|netc|toll|"
                  r"indian oil|iocl|hpcl|bpcl|hp petrol|bharat petroleum|petrol|diesel|fuels?|shell india|nayara)\b")
FUEL_PATTERN = r"\b(indian oil|iocl|hpcl|bpcl|hp petrol|bharat petroleum|petrol|diesel|fuels?|shell india|nayara)\b"
DESCRIPTION_GROUPS: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    (("fuel", "petrol", "diesel", "ईंधन", "पेट्रोल", "डीज़ल", "bensin", "bbm", "solar"), "out", FUEL_PATTERN, "fuel"),
    (("travel", "trip", "journey", "transport", "commut", "cab", "taxi", "train", "flight", "toll", "यात्रा", "सफ़र",
      "सफर", "किराया-भाड़ा", "perjalanan", "transportasi", "tol"), "out", TRAVEL_PATTERN, "travel"),
)
PROFILE_WORDS = ("occupation", "profession", "salaried", "self employed", "self-employed", "employment", "my job",
                 "what do i do for a living", "what is my work", "what is my business", "what's my business", "my business type", "type of business",
                 "पेशा", "नौकरी", "वेतनभोगी", "स्वरोज़गार", "स्वरोजगार", "क्या काम", "pekerjaan", "profesi", "karyawan",
                 "wiraswasta", "usaha saya apa", "bergaji")
SALARY_CREDIT = re.compile(r"\b(salary|sal cr|payroll|wages)\b", re.I)

PRIVATE_WORDS = ("score", "band", "rating", "risk", "fraud", "tamper", "forg", "approval chance", "will i get",
                 "approved?", "neighbour", "neighbor", "someone else", "other customer", "other people",
                 "स्कोर", "रेटिंग", "जोखिम", "धोखा", "दूसरे", "पड़ोसी", "skor", "risiko", "penipuan", "orang lain",
                 "tetangga")
ADVICE_WORDS = ("invest", "stock market", "shares", "mutual fund", "crypto", "bitcoin", "should i buy",
                "निवेश", "शेयर", "म्यूचुअल", "investasi", "saham", "reksa dana")

ACTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("concentration", ("concentrat", "depend on one", "depends on one", "single customer", "one payer", "one customer",
                       "एक ग्राहक", "satu pelanggan", "bergantung")),
    ("top_parties", ("top customer", "biggest customer", "main customer", "largest customer", "top payer", "who pays",
                     "top supplier", "biggest supplier", "main supplier", "top payee", "who do i pay", "who did i pay",
                     "who do they pay", "who did they pay", "paid the most", "pay the most", "pays the most",
                     "सबसे बड़े ग्राहक", "सबसे ज़्यादा भुगतान", "किसे", "pelanggan terbesar", "pemasok terbesar",
                     "paling banyak bayar", "siapa yang")),
    ("period", ("what period", "statement period", "period does", "date range", "cover", "how many transactions are",
                "number of transactions in", "किस अवधि", "अवधि", "periode", "rentang tanggal")),
    ("share", ("share of", "what share", "cash share", "upi share", "percentage", "percent", "proportion", "% of",
               "हिस्सा", "प्रतिशत", "porsi", "persentase", "berapa persen")),
    ("health", ("bounce", "bounced", "returned", "account health", "low balance day", "very low balance",
                "overdraw", "बाउंस", "लौट", "ditolak", "saldo rendah", "kesehatan")),
    ("breakdown", ("breakdown", "where does my money", "where did my money", "where does the money",
                   "where did the money", "where does money", "money go", "money went", "by category", "categories", "split",
                   "बंटवारा", "किस पर", "कहाँ जाता", "कहां जाता", "कहाँ गया", "कहां गया", "कहाँ से आता", "rincian",
                   "kategori", "ke mana uang", "kemana uang")),
    ("trend", ("going up", "going down", "trend", "increas", "grow", "rising", "falling", "declin", "बढ़", "घट",
               "naik", "turun", "tren")),
    ("count", ("how many", "number of", "count", "कितनी बार", "कितने लेन", "berapa kali", "berapa banyak", "jumlah transaksi")),
    ("average", ("average", "avg", "mean", "typical", "per month", "औसत", "rata-rata", "rata rata")),
    ("largest", ("biggest", "largest", "highest", "maximum", "max ", "top ", "most ", "सबसे बड़", "सबसे ज़्यादा",
                 "सबसे ज्यादा", "terbesar", "tertinggi", "paling besar")),
    ("smallest", ("smallest", "lowest", "least", "minimum", "सबसे कम", "सबसे छोट", "terkecil", "terendah", "paling kecil")),
    ("list", ("show", "list", "which transaction", "what came in", "what went out", "find", "search", "all transaction",
              "payments to", "payments from", "transactions above", "transactions over", "दिखा", "सूची", "tampilkan",
              "daftar", "cari")),
    ("total", ("how much", "total", "sum", "कितना", "कितने रुपये", "कुल", "berapa")),
)
WHICH_MONTH = ("which month", "what month", "कौन सा महीना", "किस महीने", "bulan apa", "bulan mana")

CATEGORY_LABELS = {
    "en": {"upi_credit": "UPI receipts", "neft_imps_credit": "bank transfers in", "card_settlement": "card sales",
           "cash_deposit": "cash deposits", "other_credit": "other money in", "loan_disbursal": "loan money received",
           "self_transfer": "transfers between your accounts", "nach_return": "returned instalments",
           "emi_debit": "loan instalments (EMIs)", "return_charges": "bounce charges", "supplier_payment": "supplier and other payments",
           "rent": "rent", "utilities": "bills and utilities", "staff_salary": "staff salaries",
           "cash_withdrawal": "cash withdrawals", "bank_charges": "bank charges", "tax_payment": "taxes",
           "other_debit": "other payments"},
    "hi": {"upi_credit": "UPI से आमदनी", "neft_imps_credit": "बैंक ट्रांसफ़र से आमदनी", "card_settlement": "कार्ड से बिक्री",
           "cash_deposit": "नकद जमा", "other_credit": "अन्य आमदनी", "loan_disbursal": "लोन की रकम",
           "self_transfer": "अपने खातों के बीच ट्रांसफ़र", "nach_return": "लौटी किश्तें",
           "emi_debit": "लोन की किश्तें (EMI)", "return_charges": "बाउंस शुल्क", "supplier_payment": "सप्लायर और अन्य भुगतान",
           "rent": "किराया", "utilities": "बिल", "staff_salary": "स्टाफ़ वेतन", "cash_withdrawal": "नकद निकासी",
           "bank_charges": "बैंक शुल्क", "tax_payment": "टैक्स", "other_debit": "अन्य भुगतान"},
    "id": {"upi_credit": "penerimaan UPI", "neft_imps_credit": "transfer bank masuk", "card_settlement": "penjualan kartu",
           "cash_deposit": "setoran tunai", "other_credit": "uang masuk lainnya", "loan_disbursal": "pencairan pinjaman",
           "self_transfer": "transfer antar rekening Anda", "nach_return": "cicilan yang dikembalikan",
           "emi_debit": "cicilan pinjaman (EMI)", "return_charges": "biaya penolakan", "supplier_payment": "pembayaran pemasok dan lainnya",
           "rent": "sewa", "utilities": "tagihan", "staff_salary": "gaji staf", "cash_withdrawal": "tarik tunai",
           "bank_charges": "biaya bank", "tax_payment": "pajak", "other_debit": "pembayaran lainnya"},
}

T = {
    "en": {
        "in": "received", "out": "paid out", "both": "moved through your account",
        "total": "You {verb} ₹{amount} {what} across {n} transaction(s){period}.",
        "count": "There were {n} transaction(s) {what}{period}, totalling ₹{amount}.",
        "none": "I couldn't find any transactions {what}{period} in your statement.",
        "list": "{n} transaction(s) {what}{period}, totalling ₹{amount}. {shown}",
        "shown_all": "Here they are:", "shown_some": "Here are {k} of them ({order}):",
        "by_amount": "largest first", "by_date": "most recent first",
        "largest": "Your largest {kind} {what}{period} was ₹{amount} on {day}: {desc}.",
        "smallest": "Your smallest {kind} {what}{period} was ₹{amount} on {day}: {desc}.",
        "kind_in": "receipt", "kind_out": "payment",
        "average": "On average, {what} came to ₹{monthly} a month over {months} full month(s){period}, about ₹{each} per transaction.",
        "month_high": "{month} had the highest {what}: ₹{amount}.",
        "month_low": "{month} had the lowest {what}: ₹{amount}.",
        "partial": " ({months} only partly covered by the statement, so left out of the comparison.)",
        "trend_up": "Your {what} has been going up: from ₹{first} a month in the earlier months to ₹{last} in the later months ({change}).",
        "trend_down": "Your {what} has been going down: from ₹{first} a month in the earlier months to ₹{last} in the later months ({change}).",
        "trend_flat": "Your {what} has been fairly steady: about ₹{first} a month earlier and ₹{last} later ({change}).",
        "trend_short": "Your statement doesn't cover enough full months to see a trend yet.",
        "monthly": "Month by month: {months}.",
        "breakdown_out": "Where your money went{period}: {parts}. Total paid out ₹{amount}.",
        "breakdown_in": "Where your money came from{period}: {parts}. Total received ₹{amount}.",
        "bal_avg": "Your average daily balance{period} was ₹{amount}.",
        "bal_low": "Your lowest balance{period} was ₹{amount}, on {day}.",
        "bal_high": "Your highest balance{period} was ₹{amount}, on {day}.",
        "bal_close": "Your balance at the end of the statement ({day}) was ₹{amount}.",
        "health_bounce": "{n} loan instalment(s) were returned unpaid{period}, in {months}.",
        "health_nobounce": "No loan instalments were returned unpaid{period}.",
        "health_charges": "Bounce charges came to ₹{amount}.",
        "health_low": "Your balance was below ₹{threshold} on {days} of {total} days.",
        "health_tip": "Keeping a buffer in the account before payment dates helps payments go through.",
        "emi_net": " {n} of these came back unpaid (₹{returned}), so ₹{net} actually reached the lender(s).",
        "share_in": "{what} made up {share} of the money you received{period}: ₹{amount} of ₹{whole}.",
        "party_item": "{name} ₹{amount} ({n} payments, {share})",
        "pooled_cash": "cash deposits (not traceable to a payer)", "pooled_settlement": "UPI/card settlements (many customers, pooled)",
        "pooled_note": " {share} of income arrives pooled (UPI/card settlements, cash) and cannot be traced to one payer.",
        "all_pooled": "Your income arrives pooled ({share} through UPI/card settlements and cash), so it is spread across many customers rather than one payer.",
        "top_in": "Who paid you the most: {parts}.", "top_out": "Who you paid the most: {parts}.",
        "concentration": "Your largest payer, {name}, gave {share} of your business income (₹{amount} of ₹{whole}); the top three together {share3}, out of {payers} payers.",
        "period_answer": "Your statement covers {start} to {end} ({months} months): {n} transactions, opening balance ₹{opening}, closing balance ₹{closing}.",
        "share_out": "{what} made up {share} of your payments{period}: ₹{amount} of ₹{whole}.",
        "travel": "travel (fuel, tolls, rail, air, cabs, hotels)", "fuel": "fuel",
        "spoken_rows": "The main ones: {items}.", "spoken_one": "It was {items}.", "spoken_item": "₹{amount} on {day} ({desc})",
        "profile": "This application is for {business} ({details}).", "since": "since {x}",
        "profile_none": "I don't have your business details on this application.",
        "salaried": "Your statement shows {n} salary credit(s) totalling ₹{amount}, so it includes salaried income.",
        "self_employed": "Your statement shows business income, mainly {sources}, and no salary credits, so it looks self-employed rather than salaried.",
        "no_income_seen": "Your statement doesn't show regular salary or business income.", "and": " and ",
        "income": "business income", "income_total": "in business income", "spending": "spending", "money_in": "of money received", "money_out": "of payments",
        "for": "in {x}", "via": "through {x}", "to": "to {x}", "from": "from {x}", "above": "above ₹{x}", "below": "below ₹{x}",
        "period_month": " in {x}", "period_range": " between {a} and {b}", "period_day": " on {x}",
        "private": "I can't share internal assessments or anything about other people. I can answer questions about the money in and out of your own statement.",
        "advice": "I can't give investment advice. I can show you what your statement says, for example your average balance or monthly income.",
        "redirect": "I can only answer questions about the transactions in your statement.",
        "unclear": "I'm not sure what you'd like to know. You could ask, for example:",
        "not_found_party": "I couldn't find any transactions mentioning \"{x}\" in your statement.",
        "follow_ups": ["How much did I receive through UPI?", "Which month had the lowest income?",
                       "Where does my money go?", "What was my lowest balance?"],
    },
    "hi": {
        "in": "प्राप्त किए", "out": "भुगतान किए", "both": "आपके खाते से गुज़रे",
        "total": "आपने{period} {what} ₹{amount} {verb}, {n} लेन-देन में।",
        "count": "{period} {what} {n} लेन-देन हुए, कुल ₹{amount}।",
        "none": "आपके स्टेटमेंट में{period} {what} कोई लेन-देन नहीं मिला।",
        "list": "{period} {what} {n} लेन-देन, कुल ₹{amount}। {shown}",
        "shown_all": "ये रहे:", "shown_some": "इनमें से {k} ({order}):",
        "by_amount": "सबसे बड़े पहले", "by_date": "सबसे नए पहले",
        "largest": "{period} {what} सबसे बड़ा {kind} ₹{amount} का था, {day} को: {desc}।",
        "smallest": "{period} {what} सबसे छोटा {kind} ₹{amount} का था, {day} को: {desc}।",
        "kind_in": "प्राप्त लेन-देन", "kind_out": "भुगतान",
        "average": "औसतन {what} हर महीने ₹{monthly} रही, {months} पूरे महीनों में{period}; हर लेन-देन लगभग ₹{each}।",
        "month_high": "{month} में {what} सबसे ज़्यादा रही: ₹{amount}।",
        "month_low": "{month} में {what} सबसे कम रही: ₹{amount}।",
        "partial": " ({months} स्टेटमेंट में पूरे शामिल नहीं हैं, इसलिए तुलना से बाहर रखे गए।)",
        "trend_up": "आपकी {what} बढ़ रही है: शुरुआती महीनों में लगभग ₹{first} प्रति माह से बाद के महीनों में ₹{last} ({change})।",
        "trend_down": "आपकी {what} घट रही है: शुरुआती महीनों में लगभग ₹{first} प्रति माह से बाद के महीनों में ₹{last} ({change})।",
        "trend_flat": "आपकी {what} लगभग स्थिर है: पहले लगभग ₹{first} प्रति माह और बाद में ₹{last} ({change})।",
        "trend_short": "रुझान देखने के लिए स्टेटमेंट में अभी पर्याप्त पूरे महीने नहीं हैं।",
        "monthly": "महीने के हिसाब से: {months}।",
        "breakdown_out": "आपका पैसा कहाँ गया{period}: {parts}। कुल भुगतान ₹{amount}।",
        "breakdown_in": "आपका पैसा कहाँ से आया{period}: {parts}। कुल प्राप्त ₹{amount}।",
        "bal_avg": "आपका औसत दैनिक बैलेंस{period} ₹{amount} था।",
        "bal_low": "आपका सबसे कम बैलेंस{period} ₹{amount} था, {day} को।",
        "bal_high": "आपका सबसे ज़्यादा बैलेंस{period} ₹{amount} था, {day} को।",
        "bal_close": "स्टेटमेंट के अंत ({day}) में आपका बैलेंस ₹{amount} था।",
        "health_bounce": "{period} {n} लोन किश्तें बिना भुगतान लौट गईं, {months} में।",
        "health_nobounce": "{period} कोई लोन किश्त बिना भुगतान नहीं लौटी।",
        "health_charges": "बाउंस शुल्क ₹{amount} रहे।",
        "health_low": "{total} में से {days} दिन आपका बैलेंस ₹{threshold} से कम था।",
        "health_tip": "भुगतान की तारीख से पहले खाते में कुछ रकम रखने से भुगतान समय पर हो जाते हैं।",
        "emi_net": " इनमें से {n} बिना भुगतान लौट आईं (₹{returned}), इसलिए असल में ₹{net} ही लोन देने वालों तक पहुँचे।",
        "share_in": "{period} आपको मिली रकम में {what} का हिस्सा {share} रहा: ₹{whole} में से ₹{amount}।",
        "party_item": "{name} ₹{amount} ({n} भुगतान, {share})",
        "pooled_cash": "नकद जमा (किसी एक भुगतानकर्ता से नहीं जुड़ा)", "pooled_settlement": "UPI/कार्ड सेटलमेंट (कई ग्राहक, एक साथ)",
        "pooled_note": " आमदनी का {share} एक साथ (UPI/कार्ड सेटलमेंट, नकद) आता है और किसी एक भुगतानकर्ता से नहीं जोड़ा जा सकता।",
        "all_pooled": "आपकी आमदनी एक साथ आती है ({share} UPI/कार्ड सेटलमेंट और नकद से), यानी यह किसी एक ग्राहक पर नहीं, कई ग्राहकों में बंटी है।",
        "top_in": "आपको सबसे ज़्यादा भुगतान करने वाले: {parts}।", "top_out": "जिन्हें आपने सबसे ज़्यादा भुगतान किया: {parts}।",
        "concentration": "आपके सबसे बड़े भुगतानकर्ता {name} से आपकी व्यावसायिक आमदनी का {share} आया (₹{whole} में से ₹{amount}); शीर्ष तीन से कुल {share3}, कुल {payers} भुगतानकर्ताओं में से।",
        "period_answer": "आपका स्टेटमेंट {start} से {end} तक है ({months} महीने): {n} लेन-देन, शुरुआती बैलेंस ₹{opening}, अंतिम बैलेंस ₹{closing}।",
        "share_out": "{period} आपके भुगतानों में {what} का हिस्सा {share} रहा: ₹{whole} में से ₹{amount}।",
        "travel": "यात्रा (ईंधन, टोल, रेल, हवाई, कैब, होटल)", "fuel": "ईंधन",
        "spoken_rows": "मुख्य लेन-देन: {items}।", "spoken_one": "वह लेन-देन: {items}।", "spoken_item": "{day} को ₹{amount} ({desc})",
        "profile": "यह आवेदन {business} के लिए है ({details})।", "since": "{x} से",
        "profile_none": "इस आवेदन में आपके व्यवसाय की जानकारी नहीं है।",
        "salaried": "आपके स्टेटमेंट में {n} वेतन जमा दिखते हैं, कुल ₹{amount}, यानी इसमें वेतन की आमदनी है।",
        "self_employed": "आपके स्टेटमेंट में व्यावसायिक आमदनी दिखती है, मुख्य रूप से {sources}, और कोई वेतन जमा नहीं, इसलिए यह स्वरोज़गार लगता है, नौकरी नहीं।",
        "no_income_seen": "आपके स्टेटमेंट में नियमित वेतन या व्यावसायिक आमदनी नहीं दिखती।", "and": " और ",
        "income": "व्यावसायिक आमदनी", "income_total": "व्यावसायिक आमदनी", "spending": "खर्च", "money_in": "प्राप्त रकम", "money_out": "भुगतान",
        "for": "{x} के लिए", "via": "{x} से", "to": "{x} को", "from": "{x} से", "above": "₹{x} से ऊपर", "below": "₹{x} से कम",
        "period_month": " {x} में", "period_range": " {a} से {b} के बीच", "period_day": " {x} को",
        "private": "मैं आंतरिक आकलन या दूसरे लोगों के बारे में कुछ नहीं बता सकता। मैं आपके अपने स्टेटमेंट में आए और गए पैसे के बारे में बता सकता हूँ।",
        "advice": "मैं निवेश की सलाह नहीं दे सकता। मैं बता सकता हूँ कि आपका स्टेटमेंट क्या कहता है, जैसे औसत बैलेंस या महीने की आमदनी।",
        "redirect": "मैं केवल आपके स्टेटमेंट के लेन-देन के बारे में जवाब दे सकता हूँ।",
        "unclear": "मुझे ठीक से समझ नहीं आया। आप ऐसे पूछ सकते हैं:",
        "not_found_party": "आपके स्टेटमेंट में \"{x}\" वाला कोई लेन-देन नहीं मिला।",
        "follow_ups": ["UPI से मुझे कितना मिला?", "किस महीने आमदनी सबसे कम रही?", "मेरा पैसा कहाँ जाता है?", "मेरा सबसे कम बैलेंस कितना था?"],
    },
    "id": {
        "in": "terima", "out": "bayarkan", "both": "lewat rekening Anda",
        "total": "Anda {verb} ₹{amount} {what} dalam {n} transaksi{period}.",
        "count": "Ada {n} transaksi {what}{period}, dengan total ₹{amount}.",
        "none": "Saya tidak menemukan transaksi {what}{period} di rekening koran Anda.",
        "list": "{n} transaksi {what}{period}, total ₹{amount}. {shown}",
        "shown_all": "Berikut daftarnya:", "shown_some": "Berikut {k} di antaranya ({order}):",
        "by_amount": "terbesar dulu", "by_date": "terbaru dulu",
        "largest": "{kind} terbesar Anda {what}{period} adalah ₹{amount} pada {day}: {desc}.",
        "smallest": "{kind} terkecil Anda {what}{period} adalah ₹{amount} pada {day}: {desc}.",
        "kind_in": "Penerimaan", "kind_out": "Pembayaran",
        "average": "Rata-rata {what} sebesar ₹{monthly} per bulan selama {months} bulan penuh{period}, sekitar ₹{each} per transaksi.",
        "month_high": "{month} memiliki {what} tertinggi: ₹{amount}.",
        "month_low": "{month} memiliki {what} terendah: ₹{amount}.",
        "partial": " ({months} hanya sebagian tercakup dalam rekening koran, jadi tidak dibandingkan.)",
        "trend_up": "{what} Anda naik: dari sekitar ₹{first} per bulan di bulan-bulan awal menjadi ₹{last} di bulan-bulan akhir ({change}).",
        "trend_down": "{what} Anda turun: dari sekitar ₹{first} per bulan di bulan-bulan awal menjadi ₹{last} di bulan-bulan akhir ({change}).",
        "trend_flat": "{what} Anda cukup stabil: sekitar ₹{first} per bulan di awal dan ₹{last} kemudian ({change}).",
        "trend_short": "Rekening koran Anda belum mencakup cukup bulan penuh untuk melihat tren.",
        "monthly": "Per bulan: {months}.",
        "breakdown_out": "Ke mana uang Anda pergi{period}: {parts}. Total pembayaran ₹{amount}.",
        "breakdown_in": "Dari mana uang Anda datang{period}: {parts}. Total diterima ₹{amount}.",
        "bal_avg": "Rata-rata saldo harian Anda{period} adalah ₹{amount}.",
        "bal_low": "Saldo terendah Anda{period} adalah ₹{amount}, pada {day}.",
        "bal_high": "Saldo tertinggi Anda{period} adalah ₹{amount}, pada {day}.",
        "bal_close": "Saldo Anda di akhir rekening koran ({day}) adalah ₹{amount}.",
        "health_bounce": "{n} cicilan pinjaman dikembalikan tanpa terbayar{period}, pada {months}.",
        "health_nobounce": "Tidak ada cicilan pinjaman yang dikembalikan{period}.",
        "health_charges": "Biaya penolakan sebesar ₹{amount}.",
        "health_low": "Saldo Anda di bawah ₹{threshold} pada {days} dari {total} hari.",
        "health_tip": "Menyisakan saldo sebelum tanggal pembayaran membantu pembayaran berhasil.",
        "emi_net": " {n} di antaranya dikembalikan tanpa terbayar (₹{returned}), jadi hanya ₹{net} yang benar-benar sampai ke pemberi pinjaman.",
        "share_in": "{what} adalah {share} dari uang yang Anda terima{period}: ₹{amount} dari ₹{whole}.",
        "party_item": "{name} ₹{amount} ({n} pembayaran, {share})",
        "pooled_cash": "setoran tunai (tidak terlacak ke satu pembayar)", "pooled_settlement": "penyelesaian UPI/kartu (banyak pelanggan, digabung)",
        "pooled_note": " {share} pendapatan masuk secara gabungan (penyelesaian UPI/kartu, tunai) dan tidak bisa dilacak ke satu pembayar.",
        "all_pooled": "Pendapatan Anda masuk secara gabungan ({share} lewat penyelesaian UPI/kartu dan tunai), jadi tersebar di banyak pelanggan, bukan satu pembayar.",
        "top_in": "Yang paling banyak membayar Anda: {parts}.", "top_out": "Yang paling banyak Anda bayar: {parts}.",
        "concentration": "Pembayar terbesar Anda, {name}, memberi {share} dari pendapatan usaha (₹{amount} dari ₹{whole}); tiga teratas bersama {share3}, dari {payers} pembayar.",
        "period_answer": "Rekening koran Anda mencakup {start} sampai {end} ({months} bulan): {n} transaksi, saldo awal ₹{opening}, saldo akhir ₹{closing}.",
        "share_out": "{what} adalah {share} dari pembayaran Anda{period}: ₹{amount} dari ₹{whole}.",
        "travel": "perjalanan (bensin, tol, kereta, pesawat, taksi, hotel)", "fuel": "bensin",
        "spoken_rows": "Yang utama: {items}.", "spoken_one": "Yaitu {items}.", "spoken_item": "₹{amount} pada {day} ({desc})",
        "profile": "Pengajuan ini untuk {business} ({details}).", "since": "sejak {x}",
        "profile_none": "Saya tidak memiliki data usaha Anda di pengajuan ini.",
        "salaried": "Rekening koran Anda menunjukkan {n} penerimaan gaji sebesar ₹{amount}, jadi ada penghasilan sebagai karyawan.",
        "self_employed": "Rekening koran Anda menunjukkan pendapatan usaha, terutama {sources}, tanpa penerimaan gaji, jadi tampaknya wiraswasta, bukan karyawan.",
        "no_income_seen": "Rekening koran Anda tidak menunjukkan gaji atau pendapatan usaha yang rutin.", "and": " dan ",
        "income": "pendapatan usaha", "income_total": "pendapatan usaha", "spending": "pengeluaran", "money_in": "uang masuk", "money_out": "pembayaran",
        "for": "untuk {x}", "via": "lewat {x}", "to": "ke {x}", "from": "dari {x}", "above": "di atas ₹{x}", "below": "di bawah ₹{x}",
        "period_month": " pada {x}", "period_range": " antara {a} dan {b}", "period_day": " pada {x}",
        "private": "Saya tidak bisa membagikan penilaian internal atau informasi tentang orang lain. Saya bisa menjawab pertanyaan tentang uang masuk dan keluar di rekening koran Anda.",
        "advice": "Saya tidak bisa memberi saran investasi. Saya bisa menunjukkan isi rekening koran Anda, misalnya rata-rata saldo atau pendapatan bulanan.",
        "redirect": "Saya hanya bisa menjawab pertanyaan tentang transaksi di rekening koran Anda.",
        "unclear": "Saya kurang yakin maksud Anda. Anda bisa bertanya, misalnya:",
        "not_found_party": "Saya tidak menemukan transaksi yang menyebut \"{x}\" di rekening koran Anda.",
        "follow_ups": ["Berapa yang saya terima lewat UPI?", "Bulan apa pendapatan terendah?", "Ke mana uang saya pergi?",
                       "Berapa saldo terendah saya?"],
    },
}

OFFICER_EN = {
    **T["en"],
    "total": "The applicant {verb} ₹{amount} {what} across {n} transaction(s){period}.",
    "count": "{n} transaction(s) {what}{period}, totalling ₹{amount}.",
    "none": "No transactions {what}{period} in the statement.",
    "largest": "Largest {kind} {what}{period}: ₹{amount} on {day} ({desc}).",
    "smallest": "Smallest {kind} {what}{period}: ₹{amount} on {day} ({desc}).",
    "average": "Average {what}: ₹{monthly} a month over {months} full month(s){period}; ₹{each} per transaction.",
    "trend_up": "{what} rising: ₹{first} a month in the earlier months vs ₹{last} in the later months ({change}).",
    "trend_down": "{what} falling: ₹{first} a month in the earlier months vs ₹{last} in the later months ({change}).",
    "trend_flat": "{what} steady: ₹{first} a month earlier vs ₹{last} later ({change}).",
    "trend_short": "Too few full months in the statement to read a trend.",
    "breakdown_out": "Outflows{period}: {parts}. Total ₹{amount}.",
    "breakdown_in": "Inflows{period}: {parts}. Total ₹{amount}.",
    "bal_avg": "Average daily balance{period}: ₹{amount}.",
    "bal_low": "Lowest balance{period}: ₹{amount} on {day}.",
    "bal_high": "Highest balance{period}: ₹{amount} on {day}.",
    "bal_close": "Closing balance ({day}): ₹{amount}.",
    "health_bounce": "{n} instalment(s) returned unpaid{period} ({months}).",
    "health_nobounce": "No instalments returned unpaid{period}.",
    "health_charges": "Bounce charges ₹{amount}.",
    "health_low": "Balance below ₹{threshold} on {days} of {total} days.",
    "health_tip": "",
    "emi_net": " {n} returned unpaid (₹{returned}); net ₹{net} reached the lender(s).",
    "profile": "Application: {business} ({details}).",
    "profile_none": "No business details on the application.",
    "salaried": "{n} salary credit(s) totalling ₹{amount}: salaried income present.",
    "self_employed": "Business income, mainly {sources}; no salary credits: self-employed profile.",
    "no_income_seen": "No regular salary or business income visible.",
    "not_found_party": "No transactions mentioning \"{x}\" in the statement.",
    "spoken_rows": "Top rows: {items}.", "spoken_one": "Row: {items}.",
    "unclear": "Not a statement question I can compute.",
    "share_in": "{what}: {share} of money received{period} (₹{amount} of ₹{whole}).",
    "top_in": "Top payers: {parts}.", "top_out": "Top payees: {parts}.",
    "concentration": "Largest payer {name}: {share} of business income (₹{amount} of ₹{whole}); top three {share3}; {payers} named payers.",
    "pooled_note": " {share} of income is pooled (UPI/card settlements, cash) and not attributable to a payer.",
    "all_pooled": "Income is pooled ({share} via UPI/card settlements and cash): no single-payer concentration to measure.",
    "period_answer": "Statement covers {start} to {end} ({months} months): {n} transactions; opening ₹{opening}, closing ₹{closing}.",
    "share_out": "{what}: {share} of payments{period} (₹{amount} of ₹{whole}).",
}

# --------------------------------------------------------------------------- parsing


@dataclass
class Query:
    action: str | None = None
    direction: str | None = None              # "in" | "out" | None
    pattern: str | None = None                # description regex for a group such as travel
    group: str | None = None                  # label key for that group
    categories: frozenset[str] | None = None
    channel: str | None = None                # "upi" | "transfer" | None
    counterparty: str | None = None
    start: date | None = None
    end: date | None = None
    period: str = ""                          # "", "month", "range", "day"
    period_text: tuple[str, ...] = ()
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    what_key: str | None = None               # "income" | "spending" | None
    which_month: str | None = None            # "high" | "low"
    balance: str | None = None                # "average" | "lowest" | "highest" | "closing"
    refusal: str | None = None                # "private" | "advice" | "redirect"
    notes: list[str] = field(default_factory=list)

    @property
    def understood(self) -> bool:
        return self.refusal is not None or self.action is not None


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def _amount(number: str, unit: str) -> Decimal:
    value = Decimal(number.replace(",", ""))
    unit = unit.lower()
    if unit in ("k", "thousand", "हज़ार", "हजार", "ribu"):
        value *= 1000
    elif unit in ("lakh", "lac", "lakhs", "लाख"):
        value *= 100_000
    elif unit in ("crore", "cr", "करोड़", "juta"):
        value *= 10_000_000 if unit != "juta" else 1_000_000
    return value


AMOUNT = r"₹?\s*(?:rs\.?\s*|inr\s*)?(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakhs?|lac|crore|cr|हज़ार|हजार|लाख|करोड़|ribu|juta)?\b"
ABOVE = re.compile(r"(?:above|over|more than|greater than|at least|bigger than|>|से ज़्यादा|से ज्यादा|से ऊपर|di atas|lebih dari)\s*"
                   + AMOUNT)
BELOW = re.compile(r"(?:below|under|less than|smaller than|<|से कम|di bawah|kurang dari)\s*" + AMOUNT)
LAST_N = re.compile(r"(?:last|past|previous|पिछले|terakhir)\s*(\d{1,2})\s*(?:months?|महीन[ेों]|bulan)|(\d{1,2})\s*bulan terakhir")
WORD_NUMBERS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
                "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "दो": "2", "तीन": "3", "चार": "4", "पांच": "5",
                "पाँच": "5", "छह": "6", "dua": "2", "tiga": "3", "empat": "4", "lima": "5", "enam": "6"}
DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-zऀ-ॿ]+)")
MONTH_DAY = re.compile(r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b")
NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b|\b(\d{4})-(\d{2})-(\d{2})\b")
PARTY = re.compile(r"(?:paid to|payments? to|sent to|transfers? to|received from|payments? from|money from|from|to|ke|dari)\s+"
                   r"([a-z0-9&.' ]{3,40}?)(?=\s+(?:in|on|during|last|this|above|over|below|under|since|for|between|"
                   r"pada|di|bulan)\b|[?.!,]|$)")
STOP_PARTY = {"me", "my", "you", "the", "account", "statement", "bank", "upi", "cash", "neft", "imps", "rtgs", "card",
              "rent", "salary", "suppliers", "supplier", "customers", "customer", "income", "business", "loan", "emi",
              "date", "today", "now", "go", "come", "pay", "mana", "saya", "uang", "anda"}


class Parser:
    """Turns a question into a Query, resolving dates against the statement's own period."""

    def __init__(self, period_start: date, period_end: date) -> None:
        self.period_start, self.period_end = period_start, period_end

    def parse(self, question: str) -> Query:
        q = Query()
        text = " " + re.sub(r"\s+", " ", question.lower()).strip() + " "
        text = re.sub(r"(?<=\s)(" + "|".join(WORD_NUMBERS) + r")(?=\s)", lambda m: WORD_NUMBERS[m.group(1)], text)
        if detect(question):
            q.refusal = "redirect"
            return q
        if _has(text, PRIVATE_WORDS):
            q.refusal = "private"
            return q
        if _has(text, ADVICE_WORDS):
            q.refusal = "advice"
            return q
        if _has(text, PROFILE_WORDS):
            q.action = "profile"
            return q

        for action, words in ACTIONS:
            if _has(text, words):
                q.action = action
                break
        if q.action in ("top_parties", "concentration"):
            supplier = _has(text, ("supplier", "payee", "vendor", "who do i pay", "who did i pay", "who do they pay",
                                   "who did they pay", "pay the most", "paid the most", "सप्लायर", "किसे", "pemasok",
                                   "bayar"))
            q.direction = "out" if supplier and q.action == "top_parties" else "in"
            return q
        if q.action == "period":
            return q
        if _has(text, WHICH_MONTH):
            q.which_month = "low" if _has(text, ("lowest", "least", "minimum", "worst", "सबसे कम", "terendah", "paling kecil")) else "high"
            q.action = "which_month"

        if (_has(text, ("balance", "बैलेंस", "saldo")) and _has(text, ("day", "दिन", "hari"))
                and _has(text, ("low", "कम", "rendah"))):
            q.action = "health"
        # Balance questions are their own family.
        if _has(text, ("balance", "बैलेंस", "शेष", "saldo")) and q.action not in ("health",):
            q.balance = ("lowest" if q.action == "smallest" or _has(text, ("lowest", "minimum", "सबसे कम", "terendah"))
                         else "highest" if q.action == "largest" else "average" if q.action == "average" or _has(text, ("average",))
                         else "closing")
            q.action = "balance"

        # Direction and categories.
        if _has(text, INCOME_WORDS):
            q.direction, q.categories, q.what_key = "in", frozenset(BUSINESS_CREDIT), "income"
        elif _has(text, SPEND_WORDS) and not any(_has(text, words) for words, _, _ in CATEGORY_WORDS):
            q.direction, q.what_key = "out", "spending"
        for words, direction, cats in CATEGORY_WORDS:
            if _has(text, words):
                q.direction, q.categories, q.what_key = direction, cats, None
                break
        for words, direction, pattern, group in DESCRIPTION_GROUPS:
            if _has(text, words):
                q.direction, q.pattern, q.group, q.categories, q.what_key = direction, pattern, group, None, None
                break
        # "cash" names a part of income ("what share of my income is cash"), so it wins over "income".
        if re.search(r"\bcash\b|नकद|tunai", text) and (q.categories is None or q.what_key == "income"):
            q.what_key = None
            withdrawing = _has(text, ("withdr", "निकाल", "tarik"))
            q.direction = "out" if withdrawing else "in"
            q.categories = frozenset({"cash_withdrawal"}) if withdrawing else frozenset({"cash_deposit"})
        if re.search(r"\bupi\b", text):
            q.channel = "upi"
        elif re.search(r"\b(neft|imps|rtgs|bank transfer)\b", text):
            q.channel = "transfer"
        if q.direction is None:
            if _has(text, IN_WORDS):
                q.direction = "in"
            elif _has(text, OUT_WORDS):
                q.direction = "out"

        self._amounts(text, q)
        self._dates(text, q)
        if q.action not in ("breakdown", "health", "balance", "trend", "which_month") and q.group is None:
            self._party(text, q)

        has_subject = bool(q.direction or q.categories or q.channel or q.counterparty or q.pattern or q.period
                           or q.min_amount or q.max_amount or q.what_key)
        if q.action in ("total", "count", "average", "largest", "smallest", "list") and not has_subject:
            q.action = None
        if q.action is None and (q.counterparty or q.min_amount or q.max_amount or q.period == "day"):
            q.action = "list"
        if q.action is None and (q.categories or q.channel or q.direction or q.pattern):
            q.action = "total"
        return q

    def _amounts(self, text: str, q: Query) -> None:
        if m := ABOVE.search(text):
            q.min_amount = _amount(m.group(1), m.group(2) or "")
        if m := BELOW.search(text):
            q.max_amount = _amount(m.group(1), m.group(2) or "")

    def _month_date(self, month: int) -> tuple[date, date]:
        """The occurrence of a month inside (or nearest the end of) the statement period."""
        for year in range(self.period_end.year, self.period_start.year - 2, -1):
            first = date(year, month, 1)
            last = date(year, month, calendar.monthrange(year, month)[1])
            if first <= self.period_end and last >= self.period_start:
                return first, last
        year = self.period_end.year
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])

    def _dates(self, text: str, q: Query) -> None:
        if m := LAST_N.search(text):
            n = int(m.group(1) or m.group(2))
            end_month = date(self.period_end.year, self.period_end.month, 1)
            y, mth = divmod(end_month.year * 12 + end_month.month - 1 - (n - 1), 12)
            q.start, q.end, q.period = date(y, mth + 1, 1), self.period_end, "range"
            return
        if _has(text, ("last month", "previous month", "पिछले महीने", "bulan lalu")):
            first = date(self.period_end.year, self.period_end.month, 1)
            q.start, q.end, q.period = first, self.period_end, "month"
            return
        for rx, day_group, month_group in ((DAY_MONTH, 1, 2), (MONTH_DAY, 2, 1)):
            for m in rx.finditer(text):
                month = MONTHS.get(m.group(month_group))
                day = int(m.group(day_group))
                if month and 1 <= day <= 31:
                    first, _ = self._month_date(month)
                    try:
                        q.start = q.end = date(first.year, month, day)
                    except ValueError:
                        continue
                    q.period = "day"
                    return
        if m := NUMERIC_DATE.search(text):
            try:
                if m.group(4):
                    q.start = q.end = date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
                else:
                    year = int(m.group(3)) if m.group(3) else self.period_end.year
                    year += 2000 if year < 100 else 0
                    q.start = q.end = date(year, int(m.group(2)), int(m.group(1)))
                q.period = "day"
                return
            except ValueError:
                pass
        months = [MONTHS[w] for w in re.findall(r"[a-zऀ-ॿ]+", text) if w in MONTHS and not (w == "may" and " may i" in text)]
        if months:
            first, _ = self._month_date(months[0])
            _, last = self._month_date(months[-1])
            q.start, q.end = (first, last) if first <= last else (last, first)
            q.period = "month" if len(set(months)) == 1 else "range"

    def _party(self, text: str, q: Query) -> None:
        for m in PARTY.finditer(text):
            name = m.group(1).strip(" .'")
            # "from X" / "to X" is the clearest signal of direction, stronger than words like "payment".
            q.direction = "in" if re.search(r"\b(from|received|dari)\b", m.group(0)) else "out"
            words = [w for w in name.split() if w not in STOP_PARTY and w not in MONTHS and not w.isdigit()]
            if words and sum(len(w) for w in words) >= 3:
                q.counterparty = " ".join(words)
                return


_CHANNEL_TOKENS = {"UPI", "CR", "DR", "NEFT", "IMPS", "RTGS", "P2A", "P2M", "ACH", "D", "ECS", "NACH", "BILLPAY",
                   "POS", "ATM", "WDL", "TO", "BY", "FROM", "TRF", "TRANSFER", "INB", "MB"}


POOLED = re.compile(r"\b(SETTLEMENT|SETTL|COLLECT|PAYOUT|MERCHANT BATCH)\b")


def pooled_key(t: Transaction) -> str | None:
    """Money that arrives in bulk (UPI or card settlements, cash) cannot be traced to one payer."""
    if t.category == "cash_deposit":
        return "pooled_cash"
    if t.credit > 0 and POOLED.search(t.narration.upper()):
        return "pooled_settlement"
    return None


def party_name(narration: str) -> str:
    """The counterparty in a statement description, or the description itself when none can be told apart."""
    for part in re.split(r"[/]|\s-\s|-(?=[A-Z])", narration.upper()):
        words = [w for w in part.split() if w not in _CHANNEL_TOKENS]
        name = " ".join(words).strip(" -.")
        if (len(name) >= 3 and re.search(r"[A-Z]{3}", name) and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", name)
                and not re.fullmatch(r"[\d\sX]+", name)):
            return name
    return narration.upper()[:40]


# --------------------------------------------------------------------------- answering


@dataclass
class Answer:
    text: str
    figures: dict[str, Any] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    understood: bool = True
    author: str = "statement_engine"

    def as_dict(self) -> dict[str, Any]:
        return {"answer": self.text, "figures": self.figures, "evidence_ids": self.evidence_ids, "rows": self.rows,
                "follow_ups": self.follow_ups, "understood": self.understood, "author": self.author}


def _money(value: Decimal | float) -> str:
    return inr(Decimal(str(value)).quantize(Decimal("1")))


def _amount_of(t: Transaction) -> Decimal:
    return t.credit if t.credit > 0 else t.debit


def _month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def _month_name(key: str, lang: str) -> str:
    year, month = int(key[:4]), int(key[5:])
    names = {"en": calendar.month_name[month],
             "hi": ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"][month - 1],
             "id": ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober",
                    "November", "Desember"][month - 1]}
    return f"{names[lang]} {year}"


def _day(d: date, lang: str) -> str:
    return f"{d.day} {_month_name(_month_key(d), lang)}"


class StatementQA:
    def __init__(self, case: Case, lang: str = "en", spoken: bool = False, audience: str = "customer") -> None:
        """`spoken` is for the video guide: there is no table to point at, so the top rows are read out.
        `audience="officer"` is for the credit officer's co-pilot: third person and terse, rows named inline
        (they also highlight on the statement viewer)."""
        self.audience = audience
        self.spoken = spoken or audience == "officer"
        self.lang = "en" if audience == "officer" else (lang if lang in LANGS else "en")
        self.t = OFFICER_EN if audience == "officer" else T[self.lang]
        self.txns = sorted(case.transactions, key=lambda t: (t.txn_date, t.doc_id, t.row_index))
        header = case.documents[0].header if case.documents else None
        self.start = (header.period_from if header and header.period_from else None) or (self.txns[0].txn_date if self.txns else date.today())
        self.end = (header.period_to if header and header.period_to else None) or (self.txns[-1].txn_date if self.txns else date.today())
        self.opening = header.opening_balance if header and header.opening_balance is not None else None
        self.case = case

    # ---- helpers
    def _coverage(self) -> dict[str, float]:
        days: dict[str, int] = defaultdict(int)
        for i in range((self.end - self.start).days + 1):
            days[_month_key(self.start + timedelta(days=i))] += 1
        return {m: n / calendar.monthrange(int(m[:4]), int(m[5:]))[1] for m, n in days.items()}

    def _party_unknown(self, q: Query) -> bool:
        if not q.counterparty:
            return False
        words = q.counterparty.split()
        return not any(all(w in t.narration.lower() for w in words) for t in self.txns)

    def _select(self, q: Query) -> list[Transaction]:
        out = []
        party = q.counterparty.split() if q.counterparty else None
        for t in self.txns:
            is_in = t.credit > 0
            if q.direction == "in" and not is_in or q.direction == "out" and is_in:
                continue
            if q.categories is not None and t.category not in q.categories:
                continue
            if q.categories is None and q.direction == "out" and t.category == "self_transfer":
                continue
            narration = t.narration.lower()
            if q.channel == "upi" and "upi" not in narration:
                continue
            if q.channel == "transfer" and not re.search(r"\b(neft|imps|rtgs)\b", narration):
                continue
            if party and not all(w in narration for w in party):
                continue
            if q.pattern and not re.search(q.pattern, narration):
                continue
            if q.start and t.txn_date < q.start or q.end and t.txn_date > q.end:
                continue
            amount = _amount_of(t)
            if q.min_amount is not None and amount <= q.min_amount or q.max_amount is not None and amount >= q.max_amount:
                continue
            out.append(t)
        return out

    def _what(self, q: Query, generic: bool = True) -> str:
        """What the answer is about. `generic` adds "of money received" when nothing more specific was asked;
        sentences whose verb already says it (totals, largest) pass False."""
        t = self.t
        parts = []
        if q.counterparty:
            pass
        elif q.group:
            parts.append(t["for"].format(x=t[q.group]))
        elif q.what_key and not (not generic and q.what_key == "spending"):
            parts.append(t[q.what_key if generic or q.what_key != "income" else "income_total"])
        elif q.what_key:
            pass
        elif q.categories and len(q.categories) <= 2:
            parts.append(t["for"].format(x=CATEGORY_LABELS[self.lang][sorted(q.categories)[0]]))
        if not q.counterparty and q.channel == "upi":
            parts.append(t["via"].format(x="UPI"))
        elif not q.counterparty and q.channel == "transfer":
            parts.append(t["via"].format(x="NEFT/IMPS/RTGS"))
        if q.counterparty:
            parts.append(t["from" if q.direction == "in" else "to"].format(x=q.counterparty.upper()))
        if q.min_amount is not None:
            parts.append(t["above"].format(x=_money(q.min_amount)))
        if q.max_amount is not None:
            parts.append(t["below"].format(x=_money(q.max_amount)))
        if parts or not generic:
            return " ".join(parts)
        return t["money_in"] if q.direction == "in" else t["money_out"] if q.direction == "out" else ""

    def _period(self, q: Query) -> str:
        t = self.t
        if q.period == "day" and q.start:
            return t["period_day"].format(x=_day(q.start, self.lang))
        if q.period == "month" and q.start:
            return t["period_month"].format(x=_month_name(_month_key(q.start), self.lang))
        if q.period == "range" and q.start and q.end:
            return t["period_range"].format(a=_day(max(q.start, self.start), self.lang), b=_day(min(q.end, self.end), self.lang))
        return ""

    def _row(self, t: Transaction) -> dict[str, Any]:
        return {"date": t.txn_date.isoformat(), "description": t.narration[:60], "amount": _money(_amount_of(t)),
                "direction": "in" if t.credit > 0 else "out", "page": t.page, "evidence_id": t.evidence_id}

    def _done(self, text: str, rows: list[Transaction], figures: dict[str, Any], listed: list[Transaction] | None = None) -> Answer:
        text = re.sub(r"\s+([:.,;)])", r"\1", re.sub(r"\s{2,}", " ", text)).strip()
        text = text[:1].upper() + text[1:]
        return Answer(text=text, figures=figures, evidence_ids=[r.evidence_id for r in rows][:MAX_EVIDENCE],
                      rows=[self._row(r) for r in (listed if listed is not None else rows[:MAX_ROWS])],
                      follow_ups=self.t["follow_ups"][:3])

    def _daily_balances(self) -> dict[date, Decimal]:
        if not self.txns:
            return {}
        eod: dict[date, Decimal] = {}
        for t in self.txns:
            eod[t.txn_date] = t.balance
        first = self.txns[0]
        balance = self.opening if self.opening is not None else first.balance - first.credit + first.debit
        out = {}
        for i in range((self.end - self.start).days + 1):
            d = self.start + timedelta(days=i)
            balance = eod.get(d, balance)
            out[d] = balance
        return out

    # ---- entry point
    def answer(self, question: str) -> Answer:
        t = self.t
        q = Parser(self.start, self.end).parse(question)
        if not self.txns and q.action != "profile":
            return Answer(text=t["none"].format(what="", period=""), understood=False)
        if q.refusal:
            return Answer(text=t[q.refusal], follow_ups=t["follow_ups"][:3], understood=True, author="statement_engine")
        if not q.understood:
            return Answer(text=f"{t['unclear']} " + " · ".join(t["follow_ups"]), follow_ups=t["follow_ups"], understood=False)
        handler = getattr(self, f"_a_{q.action}")
        return handler(q)

    # ---- actions
    def _a_profile(self, q: Query) -> Answer:
        """The customer's own application details, and whether the statement shows salary or business income.
        Reads only what the customer entered and their transactions, never anything the lender derived."""
        t, a = self.t, self.case.applicant
        parts = []
        if a is not None:
            details = [a.constitution.value.replace("_", " "), a.industry]
            if a.business_start_year:
                details.append(t["since"].format(x=a.business_start_year))
            parts.append(t["profile"].format(business=a.business_name, details=", ".join(details)))
        else:
            parts.append(t["profile_none"])
        salary = [r for r in self.txns if r.credit > 0 and SALARY_CREDIT.search(r.narration)]
        business = [r for r in self.txns if r.credit > 0 and r.category in BUSINESS_CREDIT and r not in salary]
        figures: dict[str, Any] = {"salary_credits": len(salary), "business_credits": len(business)}
        if salary:
            amount = sum((r.credit for r in salary), Decimal("0"))
            parts.append(t["salaried"].format(n=len(salary), amount=_money(amount)))
            figures["salary_total"] = float(amount)
        elif business:
            by_cat: dict[str, Decimal] = defaultdict(Decimal)
            for r in business:
                by_cat[r.category] += r.credit
            top = [CATEGORY_LABELS[self.lang][c] for c, _ in sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)[:2]]
            parts.append(t["self_employed"].format(sources=t["and"].join(top)))
        elif self.txns:
            parts.append(t["no_income_seen"])
        return self._done(" ".join(parts), salary or business[:MAX_EVIDENCE], figures, listed=(salary or [])[:MAX_ROWS])

    def _a_total(self, q: Query) -> Answer:
        if self._party_unknown(q):
            return Answer(text=self.t["not_found_party"].format(x=q.counterparty), follow_ups=self.t["follow_ups"][:3])
        rows = self._select(q)
        what, period = self._what(q, generic=False), self._period(q)
        if not rows:
            return self._done(self.t["none"].format(what=self._what(q), period=period), [], {"total": 0, "count": 0})
        total = sum((_amount_of(r) for r in rows), Decimal("0"))
        verb = self.t["in"] if q.direction == "in" else self.t["out"] if q.direction == "out" else self.t["both"]
        text = self.t["total"].format(verb=verb, amount=_money(total), what=what, n=len(rows), period=period)
        figures: dict[str, Any] = {"total": float(total), "count": len(rows)}
        if q.categories == frozenset({"emi_debit"}):
            # An EMI debit that bounced comes back as a NACH return; "paid" must not count it.
            returned = [r for r in self.txns if r.category == "nach_return"
                        and (not q.start or r.txn_date >= q.start) and (not q.end or r.txn_date <= q.end)]
            if returned:
                back = sum((r.credit for r in returned), Decimal("0"))
                text += self.t["emi_net"].format(n=len(returned), returned=_money(back), net=_money(total - back))
                figures.update(returned=len(returned), returned_amount=float(back), net_paid=float(total - back))
                rows = rows + returned
        return self._done(text.replace("  ", " "), rows, figures,
                          listed=sorted(rows, key=_amount_of, reverse=True)[:MAX_ROWS])

    def _a_share(self, q: Query) -> Answer:
        """What part of all money in (or out) a category, channel or counterparty makes up."""
        direction = q.direction or "in"
        part_q = Query(**{**q.__dict__, "direction": direction})
        whole_q = Query(direction=direction, start=q.start, end=q.end, period=q.period)
        if direction == "in" and q.categories is not None and q.categories <= BUSINESS_CREDIT:
            whole_q.categories = frozenset(BUSINESS_CREDIT)  # cash share of income, not of every credit
        part, whole = self._select(part_q), self._select(whole_q)
        what, period = self._part_label(part_q), self._period(q)
        whole_total = sum((_amount_of(r) for r in whole), Decimal("0"))
        if not part or not whole_total:
            return self._done(self.t["none"].format(what=self._what(part_q), period=period), [], {"share": 0.0})
        part_total = sum((_amount_of(r) for r in part), Decimal("0"))
        share = float(part_total / whole_total)
        key = "share_in" if direction == "in" else "share_out"
        text = self.t[key].format(what=what or self.t["money_in" if direction == "in" else "money_out"], share=f"{share:.0%}",
                                  amount=_money(part_total), whole=_money(whole_total), period=period)
        return self._done(text, part, {"share": round(share, 4), "part": float(part_total), "whole": float(whole_total)})

    def _part_label(self, q: Query) -> str:
        """A bare name for the part in a share answer: "Cash deposits", "UPI", "Rent", "INDIAN OIL"."""
        if q.counterparty:
            return q.counterparty.upper()
        if q.group:
            return self.t[q.group]
        if q.categories and len(q.categories) <= 2:
            return CATEGORY_LABELS[self.lang][sorted(q.categories)[0]]
        if q.channel == "upi":
            return "UPI"
        if q.channel == "transfer":
            return "NEFT/IMPS/RTGS"
        return ""

    def _parties(self, direction: str) -> tuple[dict[str, list[Transaction]], Decimal, dict[str, list[Transaction]]]:
        """Named counterparties, the total for the direction, and pooled money kept apart from named payers."""
        rows = [t for t in self.txns if (t.credit > 0) == (direction == "in")
                and (t.category in BUSINESS_CREDIT if direction == "in" else t.category != "self_transfer")]
        groups: dict[str, list[Transaction]] = defaultdict(list)
        pooled: dict[str, list[Transaction]] = defaultdict(list)
        for t in rows:
            key = pooled_key(t) if direction == "in" else None
            (pooled[key] if key else groups[party_name(t.narration)]).append(t)
        return groups, sum((_amount_of(t) for t in rows), Decimal("0")), pooled

    def _a_top_parties(self, q: Query) -> Answer:
        direction = q.direction or "in"
        groups, whole, pooled = self._parties(direction)
        if not whole:
            return self._done(self.t["none"].format(what="", period=""), [], {})
        ranked = sorted(groups.items(), key=lambda kv: sum(_amount_of(t) for t in kv[1]), reverse=True)[:5]
        ranked += sorted(((self.t[k], rows) for k, rows in pooled.items()),
                         key=lambda kv: sum(_amount_of(t) for t in kv[1]), reverse=True)
        parts = []
        for name, rows in ranked:
            amount = sum((_amount_of(t) for t in rows), Decimal("0"))
            parts.append(self.t["party_item"].format(name=name, amount=_money(amount), n=len(rows), share=f"{float(amount / whole):.0%}"))
        text = self.t["top_in" if direction == "in" else "top_out"].format(parts="; ".join(parts))
        used = [t for _, rows in ranked for t in rows]
        return self._done(text, used, {"top": {name: float(sum(_amount_of(t) for t in rows)) for name, rows in ranked},
                                       "whole": float(whole)},
                          listed=sorted(used, key=_amount_of, reverse=True)[:MAX_ROWS])

    def _a_concentration(self, q: Query) -> Answer:
        groups, whole, pooled = self._parties("in")
        pooled_total = sum((_amount_of(t) for rows in pooled.values() for t in rows), Decimal("0"))
        if not whole:
            return self._done(self.t["none"].format(what="", period=""), [], {})
        pooled_note = (self.t["pooled_note"].format(share=f"{float(pooled_total / whole):.0%}") if pooled_total else "")
        if not groups:
            return self._done(self.t["all_pooled"].format(share=f"{float(pooled_total / whole):.0%}"),
                              [t for rows in pooled.values() for t in rows][:MAX_EVIDENCE],
                              {"pooled_share": round(float(pooled_total / whole), 4), "payers": 0})
        ranked = sorted(groups.items(), key=lambda kv: sum(_amount_of(t) for t in kv[1]), reverse=True)
        top_name, top_rows = ranked[0]
        top = sum((_amount_of(t) for t in top_rows), Decimal("0"))
        top3 = sum((_amount_of(t) for _, rows in ranked[:3] for t in rows), Decimal("0"))
        share, share3 = float(top / whole), float(top3 / whole)
        text = self.t["concentration"].format(name=top_name, share=f"{share:.0%}", amount=_money(top), whole=_money(whole),
                                              share3=f"{share3:.0%}", payers=len(groups)) + pooled_note
        return self._done(text, top_rows, {"top_payer": top_name, "top_share": round(share, 4),
                                           "top3_share": round(share3, 4), "payers": len(groups),
                                           "pooled_share": round(float(pooled_total / whole), 4)})

    def _a_period(self, q: Query) -> Answer:
        from app.cashflow.features import span_months

        daily = self._daily_balances()
        opening = daily[min(daily)] if daily else Decimal("0")
        closing = daily[max(daily)] if daily else Decimal("0")
        text = self.t["period_answer"].format(start=_day(self.start, self.lang), end=_day(self.end, self.lang),
                                              months=f"{span_months(self.start, self.end):.1f}", n=len(self.txns),
                                              opening=_money(opening), closing=_money(closing))
        return self._done(text, [], {"from": self.start.isoformat(), "to": self.end.isoformat(),
                                     "months": span_months(self.start, self.end), "transactions": len(self.txns)})

    def _a_count(self, q: Query) -> Answer:
        rows = self._select(q)
        what, period = self._what(q), self._period(q)
        if not rows:
            return self._done(self.t["none"].format(what=what, period=period), [], {"count": 0})
        total = sum((_amount_of(r) for r in rows), Decimal("0"))
        return self._done(self.t["count"].format(n=len(rows), what=what, period=period, amount=_money(total)).replace("  ", " "),
                          rows, {"count": len(rows), "total": float(total)})

    def _a_list(self, q: Query) -> Answer:
        if self._party_unknown(q):
            return Answer(text=self.t["not_found_party"].format(x=q.counterparty), follow_ups=self.t["follow_ups"][:3])
        rows = self._select(q)
        what, period = self._what(q), self._period(q)
        if not rows:
            return self._done(self.t["none"].format(what=what, period=period), [], {"count": 0})
        by_amount = q.min_amount is not None or q.max_amount is not None
        ordered = sorted(rows, key=_amount_of, reverse=True) if by_amount else sorted(rows, key=lambda r: r.txn_date, reverse=True)
        shown = (self.t["shown_all"] if len(rows) <= MAX_ROWS else
                 self.t["shown_some"].format(k=MAX_ROWS, order=self.t["by_amount" if by_amount else "by_date"]))
        if self.spoken:
            items = [self.t["spoken_item"].format(amount=_money(_amount_of(r)), day=_day(r.txn_date, self.lang),
                                                  desc=r.narration[:40]) for r in ordered[:3]]
            shown = self.t["spoken_rows" if len(items) > 1 else "spoken_one"].format(items="; ".join(items))
        total = sum((_amount_of(r) for r in rows), Decimal("0"))
        text = self.t["list"].format(n=len(rows), what=what, period=period, amount=_money(total), shown=shown)
        return self._done(text.replace("  ", " "), rows, {"count": len(rows), "total": float(total)}, listed=ordered[:MAX_ROWS])

    def _extreme(self, q: Query, largest: bool) -> Answer:
        if q.direction is None and q.categories is None and not q.channel:
            q.direction = "in"
        rows = self._select(q)
        what, period = self._what(q, generic=False), self._period(q)
        if not rows:
            return self._done(self.t["none"].format(what=self._what(q), period=period), [], {})
        pick = (max if largest else min)(rows, key=_amount_of)
        text = self.t["largest" if largest else "smallest"].format(
            kind=self.t["kind_in" if pick.credit > 0 else "kind_out"], what=what, period=period, amount=_money(_amount_of(pick)), day=_day(pick.txn_date, self.lang), desc=pick.narration[:60])
        top = sorted(rows, key=_amount_of, reverse=largest)[:5]
        return self._done(text.replace("  ", " "), [pick], {"amount": float(_amount_of(pick))}, listed=top)

    def _a_largest(self, q: Query) -> Answer:
        return self._extreme(q, True)

    def _a_smallest(self, q: Query) -> Answer:
        return self._extreme(q, False)

    def _monthly(self, q: Query) -> tuple[dict[str, Decimal], list[str], list[str], list[Transaction]]:
        if q.direction is None and q.categories is None and not q.channel:
            q.direction, q.categories, q.what_key = "in", frozenset(BUSINESS_CREDIT), "income"
        rows = self._select(q)
        coverage = self._coverage()
        totals: dict[str, Decimal] = {m: Decimal("0") for m in coverage
                                      if (not q.start or m >= _month_key(q.start)) and (not q.end or m <= _month_key(q.end))}
        for r in rows:
            totals[_month_key(r.txn_date)] = totals.get(_month_key(r.txn_date), Decimal("0")) + _amount_of(r)
        full = [m for m in sorted(totals) if coverage.get(m, 0) >= FULL_MONTH]
        partial = [m for m in sorted(totals) if coverage.get(m, 0) < FULL_MONTH]
        return totals, full, partial, rows

    def _a_average(self, q: Query) -> Answer:
        totals, full, _, rows = self._monthly(q)
        what, period = self._what(q), self._period(q)
        if not rows or not full:
            return self._done(self.t["none"].format(what=what, period=period), [], {})
        monthly = sum((totals[m] for m in full), Decimal("0")) / len(full)
        in_full = [r for r in rows if _month_key(r.txn_date) in full]
        each = sum((_amount_of(r) for r in in_full), Decimal("0")) / max(1, len(in_full))
        text = self.t["average"].format(what=what, monthly=_money(monthly), months=len(full), period=period, each=_money(each))
        return self._done(text.replace("  ", " "), in_full, {"monthly_average": float(monthly), "per_transaction": float(each),
                                                             "months": len(full)})

    def _a_which_month(self, q: Query) -> Answer:
        totals, full, partial, rows = self._monthly(q)
        what = self._what(q)
        if not full:
            return self._done(self.t["trend_short"], [], {})
        pick = (max if q.which_month == "high" else min)(full, key=lambda m: totals[m])
        text = self.t["month_high" if q.which_month == "high" else "month_low"].format(
            month=_month_name(pick, self.lang), what=what, amount=_money(totals[pick]))
        if partial:
            text += self.t["partial"].format(months=", ".join(_month_name(m, self.lang) for m in partial))
        text += " " + self.t["monthly"].format(months=", ".join(f"{_month_name(m, self.lang)} ₹{_money(totals[m])}" for m in full))
        month_rows = [r for r in rows if _month_key(r.txn_date) == pick]
        return self._done(text, month_rows, {"month": pick, "amount": float(totals[pick]),
                                             "by_month": {m: float(totals[m]) for m in full}})

    def _a_trend(self, q: Query) -> Answer:
        totals, full, partial, rows = self._monthly(q)
        what = self._what(q)
        if len(full) < 2:
            return self._done(self.t["trend_short"], [], {})
        half = max(1, len(full) // 2)
        first = sum((totals[m] for m in full[:half]), Decimal("0")) / half
        last = sum((totals[m] for m in full[-half:]), Decimal("0")) / half
        change = float((last - first) / first) if first else 0.0
        key = "trend_up" if change > 0.05 else "trend_down" if change < -0.05 else "trend_flat"
        text = self.t[key].format(what=what, first=_money(first), last=_money(last), change=f"{change:+.0%}")
        text += " " + self.t["monthly"].format(months=", ".join(f"{_month_name(m, self.lang)} ₹{_money(totals[m])}" for m in full))
        if partial:
            text += self.t["partial"].format(months=", ".join(_month_name(m, self.lang) for m in partial))
        return self._done(text, rows, {"earlier_monthly": float(first), "later_monthly": float(last), "change": round(change, 4),
                                       "by_month": {m: float(totals[m]) for m in full}})

    def _a_breakdown(self, q: Query) -> Answer:
        direction = q.direction or "out"
        q.direction, q.categories = direction, None
        rows = [r for r in self._select(q) if r.category != "self_transfer"]
        period = self._period(q)
        if not rows:
            return self._done(self.t["none"].format(what="", period=period), [], {})
        by_cat: dict[str, Decimal] = defaultdict(Decimal)
        for r in rows:
            by_cat[r.category] += _amount_of(r)
        total = sum(by_cat.values(), Decimal("0"))
        ranked = sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)
        parts = ", ".join(f"{CATEGORY_LABELS[self.lang].get(c, c)} ₹{_money(v)} ({float(v / total):.0%})" for c, v in ranked[:6])
        text = self.t["breakdown_in" if direction == "in" else "breakdown_out"].format(period=period, parts=parts, amount=_money(total))
        return self._done(text, rows, {"total": float(total), "by_category": {c: float(v) for c, v in ranked}},
                          listed=sorted(rows, key=_amount_of, reverse=True)[:MAX_ROWS])

    def _a_balance(self, q: Query) -> Answer:
        daily = {d: b for d, b in self._daily_balances().items()
                 if (not q.start or d >= q.start) and (not q.end or d <= q.end)}
        period = self._period(q)
        if not daily:
            return self._done(self.t["none"].format(what="", period=period), [], {})
        if q.balance == "average":
            avg = sum(daily.values(), Decimal("0")) / len(daily)
            return self._done(self.t["bal_avg"].format(period=period, amount=_money(avg)), [], {"average_balance": float(avg)})
        if q.balance in ("lowest", "highest"):
            day = (min if q.balance == "lowest" else max)(daily, key=lambda d: daily[d])
            rows = [r for r in self.txns if r.txn_date == day][-1:]
            text = self.t["bal_low" if q.balance == "lowest" else "bal_high"].format(
                period=period, amount=_money(daily[day]), day=_day(day, self.lang))
            return self._done(text, rows, {"balance": float(daily[day]), "date": day.isoformat()})
        day = max(daily)
        rows = [r for r in self.txns if r.txn_date <= day][-1:]
        return self._done(self.t["bal_close"].format(day=_day(day, self.lang), amount=_money(daily[day])), rows,
                          {"balance": float(daily[day]), "date": day.isoformat()})

    def _a_health(self, q: Query) -> Answer:
        period = self._period(q)
        in_range = [r for r in self.txns if (not q.start or r.txn_date >= q.start) and (not q.end or r.txn_date <= q.end)]
        bounces = [r for r in in_range if r.category == "nach_return"]
        charges = [r for r in in_range if r.category == "return_charges"]
        parts = []
        if bounces:
            months = sorted({_month_key(r.txn_date) for r in bounces})
            parts.append(self.t["health_bounce"].format(n=len(bounces), period=period,
                                                        months=", ".join(_month_name(m, self.lang) for m in months)))
        else:
            parts.append(self.t["health_nobounce"].format(period=period))
        if charges:
            parts.append(self.t["health_charges"].format(amount=_money(sum((_amount_of(r) for r in charges), Decimal("0")))))
        # Same low-balance threshold the cash-flow features use: 5% of median monthly business income, at least 2,000.
        income_by_month: dict[str, Decimal] = defaultdict(Decimal)
        for r in self.txns:
            if r.credit > 0 and r.category in BUSINESS_CREDIT:
                income_by_month[_month_key(r.txn_date)] += r.credit
        median_income = statistics.median(income_by_month.values()) if income_by_month else Decimal("0")
        threshold = max(Decimal("2000"), Decimal(str(median_income)) * Decimal("0.05"))
        daily = {d: b for d, b in self._daily_balances().items()
                 if (not q.start or d >= q.start) and (not q.end or d <= q.end)}
        low_days = sum(1 for b in daily.values() if b < threshold)
        parts.append(self.t["health_low"].format(threshold=_money(threshold), days=low_days, total=len(daily)))
        if bounces or low_days:
            parts.append(self.t["health_tip"])
        rows = bounces + charges
        return self._done(" ".join(p.replace("  ", " ").strip() for p in parts), rows,
                          {"bounced": len(bounces), "bounce_charges": float(sum((_amount_of(r) for r in charges), Decimal("0"))),
                           "low_balance_days": low_days, "days": len(daily), "low_balance_threshold": float(threshold)})


async def answer(case: Case, question: str, language: str, gateway: LLMGateway, meter: UsageMeter) -> dict[str, Any]:
    """Answer from the statement. A configured model may only rephrase the computed answer."""
    result = StatementQA(case, language).answer(question)
    if gateway.enabled and result.understood and result.figures and not detect(question):
        facts = {"language": language, "question": question[:500], "computed_answer": result.text, "figures": result.figures}
        try:
            draft = await gateway.structured(prompt_id="statement_qa", schema=CoachDraft, meter=meter,
                                             contents=[json.dumps(facts, ensure_ascii=False, default=str)])
            allowed = set(NUMBER.findall(json.dumps(facts, ensure_ascii=False, default=str)))
            if (not INTERNAL_TERMS.search(draft.reply) and len(draft.reply) <= 700
                    and all(n in allowed for n in NUMBER.findall(draft.reply))):
                result.text, result.author = draft.reply, gateway.provider
        except LLMUnavailable:
            pass
    return result.as_dict()


FUTURE_LOAN = re.compile(r"\b(will|would|going to|new loan|my loan|this loan|interest rate|kfs|approved)\b"
                         r"|होगी|होगा|होंगे|मिलेगा|\b(akan|nanti)\b")


def understands(case: Case, question: str, language: str = "en") -> bool:
    """Whether a question is a statement question the engine can answer (used by the video guide)."""
    if FUTURE_LOAN.search(question.lower()):
        return False
    qa = StatementQA(case, language)
    parsed = Parser(qa.start, qa.end).parse(question)
    if not case.transactions:
        return parsed.action == "profile"  # answerable from the application alone
    return parsed.refusal is None and parsed.action is not None
