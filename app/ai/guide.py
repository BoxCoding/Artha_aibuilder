"""Journey guide: the voice of the video assistant that walks a customer through their application.

It answers from the journey state (app.journey.state) and approved texts, never from scores or
findings. Offline it is fully scripted; with a model configured, the model may phrase the reply,
but its draft is dropped for the scripted one if it mentions anything internal or any figure that
is not in the facts it was given. It has no tools that change anything: its actions are buttons
the customer presses themselves.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.ai.explain import EMPATHY, GLOSSARY_QUESTION, INTERNAL_TERMS, NUMBER, SAFE_REDIRECT, coach_offline
from app.ai.gateway import LLMGateway, LLMUnavailable, UsageMeter
from app.ai import statement_qa
from app.ai.schemas import CoachDraft
from app.domain.models import Case
from app.security.injection import detect

LANGS = ("en", "hi", "id")

STEP_TEXT: dict[str, dict[str, str]] = {
    "details": {
        "en": "Hello, I'm your application guide. First, fill in your details and your business details, as they "
              "appear on your PAN card. Then tick the consent box and press Save and continue.",
        "hi": "नमस्ते, मैं आपका आवेदन गाइड हूँ। पहले अपनी और अपने व्यवसाय की जानकारी भरें, जैसी आपके PAN कार्ड पर है। "
              "फिर सहमति वाले बॉक्स पर टिक करें और 'सेव करें और आगे बढ़ें' दबाएँ।",
        "id": "Halo, saya pemandu pengajuan Anda. Pertama, isi data diri dan data usaha Anda sesuai kartu PAN. "
              "Lalu centang kotak persetujuan dan tekan Simpan dan lanjutkan.",
    },
    "statements": {
        "en": "Now upload your bank statements for the last six months, as PDFs downloaded from net banking or "
              "your bank's email. You can add up to {max_documents} files. If a file has a password, I will ask "
              "you for it.",
        "hi": "अब पिछले छह महीनों के बैंक स्टेटमेंट अपलोड करें, नेट बैंकिंग या बैंक के ईमेल से डाउनलोड की गई PDF के रूप में। "
              "आप {max_documents} फ़ाइलें तक जोड़ सकते हैं। अगर फ़ाइल पर पासवर्ड है, तो मैं आपसे पूछूँगा।",
        "id": "Sekarang unggah rekening koran enam bulan terakhir, berupa PDF dari internet banking atau email bank. "
              "Anda dapat menambahkan hingga {max_documents} file. Jika file berpassword, saya akan menanyakannya.",
    },
    "submit": {
        "en": "Your statements are in: {documents} added. When you have added everything, press Submit for "
              "assessment.",
        "hi": "आपके स्टेटमेंट आ गए हैं: {documents} जोड़े गए। सब कुछ जोड़ने के बाद 'आकलन के लिए जमा करें' दबाएँ।",
        "id": "Rekening koran Anda sudah masuk: {documents} ditambahkan. Setelah semuanya lengkap, tekan Kirim "
              "untuk penilaian.",
    },
    "processing": {
        "en": "Thank you. We are reading your statements now. This usually takes less than a minute, and you can "
              "watch each step on screen.",
        "hi": "धन्यवाद। हम अभी आपके स्टेटमेंट पढ़ रहे हैं। इसमें आमतौर पर एक मिनट से कम लगता है, और आप हर चरण स्क्रीन पर "
              "देख सकते हैं।",
        "id": "Terima kasih. Kami sedang membaca rekening koran Anda. Biasanya kurang dari satu menit, dan Anda bisa "
              "melihat setiap langkah di layar.",
    },
    "review": {
        "en": "Your application is complete and is now with a loan officer. Every loan is decided by a person. "
              "You can close this page and come back to My applications at any time.",
        "hi": "आपका आवेदन पूरा हो गया है और अब लोन अधिकारी के पास है। हर लोन का फ़ैसला एक व्यक्ति करता है। आप यह पेज बंद "
              "करके कभी भी 'मेरे आवेदन' में वापस आ सकते हैं।",
        "id": "Pengajuan Anda sudah lengkap dan sekarang ada di petugas pinjaman. Setiap pinjaman diputuskan oleh "
              "petugas. Anda boleh menutup halaman ini dan kembali ke Pengajuan saya kapan saja.",
    },
    "accept_terms": {
        "en": "Good news, there is a decision. Please read your Key Fact Statement: it shows the interest rate, "
              "all charges and what you will repay. The money is sent only after you tick that you accept.",
        "hi": "अच्छी खबर, फ़ैसला आ गया है। कृपया अपना 'की फैक्ट स्टेटमेंट' पढ़ें: इसमें ब्याज दर, सभी शुल्क और कुल चुकाने "
              "की राशि है। आपके स्वीकार करने के बाद ही पैसा भेजा जाएगा।",
        "id": "Kabar baik, sudah ada keputusan. Silakan baca ringkasan informasi pinjaman Anda: berisi suku bunga, "
              "semua biaya dan total yang harus dibayar. Dana dikirim hanya setelah Anda mencentang setuju.",
    },
    "done": {
        "en": "You're all set. You accepted the terms and the loan will be paid out to your account. You can "
              "ask me about your repayments at any time.",
        "hi": "सब हो गया। आपने शर्तें स्वीकार कर ली हैं और लोन आपके खाते में भेजा जाएगा। आप अपनी किश्तों के बारे में मुझसे "
              "कभी भी पूछ सकते हैं।",
        "id": "Semua sudah selesai. Anda telah menyetujui ketentuan dan dana akan dikirim ke rekening Anda. Anda bisa "
              "bertanya tentang cicilan kapan saja.",
    },
    "decided": {
        "en": "There is a decision on your application. It is on your screen, with the reasons and what you can "
              "do next. If you would like to talk it through with a person, I can arrange a call back.",
        "hi": "आपके आवेदन पर फ़ैसला आ गया है। यह आपकी स्क्रीन पर है, कारणों और आगे क्या करें, के साथ। अगर आप किसी व्यक्ति "
              "से बात करना चाहें, तो मैं कॉल बैक का अनुरोध कर सकता हूँ।",
        "id": "Sudah ada keputusan atas pengajuan Anda. Keputusan itu ada di layar, beserta alasan dan langkah "
              "selanjutnya. Jika ingin membicarakannya dengan petugas, saya bisa mengatur panggilan balik.",
    },
}

HELP_TEXT: dict[str, dict[str, str]] = {
    "password": {
        "en": "Many banks protect statements with a password. It is usually in the email your bank sent, often "
              "made from your name, date of birth or customer ID. Type it in the box next to the file. We never "
              "store it.",
        "hi": "कई बैंक स्टेटमेंट को पासवर्ड से सुरक्षित रखते हैं। यह आमतौर पर बैंक के ईमेल में होता है, अक्सर आपके नाम, जन्म "
              "तिथि या ग्राहक ID से बना। इसे फ़ाइल के पास वाले बॉक्स में लिखें। हम इसे कभी सेव नहीं करते।",
        "id": "Banyak bank melindungi rekening koran dengan kata sandi. Biasanya ada di email dari bank, sering dari "
              "nama, tanggal lahir atau ID nasabah Anda. Ketik di kotak di samping file. Kami tidak menyimpannya.",
    },
    "scan": {
        "en": "Photos and scans can't be read automatically here. Please download the original PDF from net "
              "banking or your bank's email and upload that instead.",
        "hi": "फ़ोटो और स्कैन यहाँ अपने आप नहीं पढ़े जा सकते। कृपया नेट बैंकिंग या बैंक के ईमेल से असली PDF डाउनलोड करके "
              "वही अपलोड करें।",
        "id": "Foto dan hasil pindai tidak bisa dibaca otomatis di sini. Silakan unduh PDF asli dari internet banking "
              "atau email bank, lalu unggah file itu.",
    },
    "documents": {
        "en": "We need your bank statements for the last six months, for the account your business uses most. "
              "They show your business income, which is how we assess the loan instead of a credit score.",
        "hi": "हमें पिछले छह महीनों के बैंक स्टेटमेंट चाहिए, उस खाते के जिसे आपका व्यवसाय सबसे ज़्यादा इस्तेमाल करता है। "
              "इनसे आपकी व्यावसायिक आमदनी दिखती है, और इसी से हम लोन का आकलन करते हैं।",
        "id": "Kami perlu rekening koran enam bulan terakhir, dari rekening yang paling sering dipakai usaha Anda. "
              "Dari situ terlihat pemasukan usaha Anda, dan itulah dasar penilaian pinjaman.",
    },
    "time": {
        "en": "Reading the statements takes about a minute. A loan officer then reviews every application, "
              "usually within two working days.",
        "hi": "स्टेटमेंट पढ़ने में लगभग एक मिनट लगता है। फिर एक लोन अधिकारी हर आवेदन देखता है, आमतौर पर दो कार्य-दिवसों में।",
        "id": "Membaca rekening koran butuh sekitar satu menit. Lalu petugas pinjaman meninjau setiap pengajuan, "
              "biasanya dalam dua hari kerja.",
    },
    "private": {
        "en": "I can't share internal assessments or anything about other customers. I can tell you where your "
              "application is and what to do next.",
        "hi": "मैं आंतरिक आकलन या दूसरे ग्राहकों के बारे में कुछ नहीं बता सकता। मैं बता सकता हूँ कि आपका आवेदन कहाँ है और "
              "आगे क्या करना है।",
        "id": "Saya tidak bisa membagikan penilaian internal atau informasi nasabah lain. Saya bisa memberi tahu posisi "
              "pengajuan Anda dan langkah selanjutnya.",
    },
    "callback": {
        "en": "Of course. Tap Ask for a call back and a loan officer will call you within two working days.",
        "hi": "ज़रूर। 'कॉल बैक का अनुरोध करें' दबाएँ, दो कार्य-दिवसों में लोन अधिकारी आपको कॉल करेंगे।",
        "id": "Tentu. Ketuk Minta ditelepon dan petugas pinjaman akan menelepon Anda dalam dua hari kerja.",
    },
    "identity": {
        "en": "I'm Sahayak, an AI assistant that guides you through your loan application and answers questions about "
              "your statement. I'm not a person: a loan officer decides every application, and you can ask for a call "
              "back at any time.",
        "hi": "मैं सहायक हूँ, एक AI सहायक जो आपके लोन आवेदन में मदद करता है और आपके स्टेटमेंट के बारे में सवालों के जवाब देता है। "
              "मैं कोई व्यक्ति नहीं हूँ: हर आवेदन का फ़ैसला लोन अधिकारी करते हैं, और आप कभी भी कॉल बैक माँग सकते हैं।",
        "id": "Saya Sahayak, asisten AI yang memandu pengajuan pinjaman Anda dan menjawab pertanyaan tentang rekening "
              "koran Anda. Saya bukan manusia: setiap pengajuan diputuskan oleh petugas pinjaman, dan Anda bisa meminta "
              "ditelepon kapan saja.",
    },
    "unclear": {
        "en": "Sorry, I didn't catch that. You can ask me about your application, for example \"What do I do next?\", "
              "or about your statement, for example \"Where does my money go?\" or \"Which month had the lowest income?\"",
        "hi": "माफ़ कीजिए, मैं समझ नहीं पाया। आप अपने आवेदन के बारे में पूछ सकते हैं, जैसे \"अब मुझे क्या करना है?\", या अपने "
              "स्टेटमेंट के बारे में, जैसे \"मेरा पैसा कहाँ जाता है?\" या \"किस महीने आमदनी सबसे कम रही?\"",
        "id": "Maaf, saya kurang menangkap maksud Anda. Anda bisa bertanya tentang pengajuan, misalnya \"Apa yang harus saya "
              "lakukan?\", atau tentang rekening koran, misalnya \"Ke mana uang saya pergi?\" atau \"Bulan apa pendapatan terendah?\"",
    },
    "callback_later": {
        "en": "Of course. Once you have started your application, you can ask for a call back and a loan officer "
              "will call you.",
        "hi": "ज़रूर। आवेदन शुरू करने के बाद आप कॉल बैक का अनुरोध कर सकते हैं, और लोन अधिकारी आपको कॉल करेंगे।",
        "id": "Tentu. Setelah memulai pengajuan, Anda bisa meminta panggilan balik dan petugas pinjaman akan menelepon.",
    },
}

ACTION_LABELS: dict[str, dict[str, str]] = {
    "fill_form": {"en": "Go to the form", "hi": "फ़ॉर्म पर जाएँ", "id": "Ke formulir"},
    "upload": {"en": "Upload statements", "hi": "स्टेटमेंट अपलोड करें", "id": "Unggah rekening koran"},
    "submit": {"en": "Submit for assessment", "hi": "आकलन के लिए जमा करें", "id": "Kirim untuk penilaian"},
    "view_decision": {"en": "See the decision", "hi": "फ़ैसला देखें", "id": "Lihat keputusan"},
    "callback": {"en": "Ask for a call back", "hi": "कॉल बैक का अनुरोध करें", "id": "Minta ditelepon"},
}

KEYWORDS: dict[str, tuple[str, ...]] = {
    "identity": ("your name", "ur name", "who are you", "who r u", "are you a bot", "are you a robot", "are you human",
                 "are you real", "are you ai", "are you a person", "तुम्हारा नाम", "आपका नाम", "आप कौन", "तुम कौन",
                 "nama kamu", "nama anda", "siapa kamu", "siapa anda", "kamu robot"),
    "distress": ("worried", "stress", "hardship", "can't pay", "cannot pay", "scared", "परेशान", "चिंता", "मुश्किल",
                 "susah", "khawatir", "sulit", "takut"),
    "human": ("human", "person", "officer", "call me", "call back", "callback", "talk to", "speak to", "agent",
              "अधिकारी", "बात", "कॉल", "इंसान", "petugas", "telepon", "manusia", "hubungi"),
    "private": ("score", "band", "rating", "risk", "fraud", "neighbour", "neighbor", "someone else", "other customer",
                "other people", "स्कोर", "रेटिंग", "दूसरे", "पड़ोसी", "skor", "orang lain", "tetangga", "nasabah lain"),
    "password": ("password", "पासवर्ड", "kata sandi", "sandi", "locked", "won't open", "can't open"),
    "scan": ("scan", "photo", "picture", "camera", "फ़ोटो", "फोटो", "स्कैन", "foto", "pindai", "gambar"),
    "documents": ("document", "statement", "which file", "what file", "upload", "दस्तावेज़", "स्टेटमेंट", "अपलोड",
                  "dokumen", "rekening koran", "unggah", "berkas"),
    "time": ("how long", "when will", "how much time", "kab tak", "कब", "कितना समय", "कितने दिन", "berapa lama",
             "kapan"),
    "decision": ("why", "reason", "improve", "better", "emi", "instalment", "installment", "interest", "repay",
                 "क्यों", "कारण", "सुधार", "किश्त", "ब्याज", "चुका", "kenapa", "alasan", "perbaiki", "cicilan", "bunga"),
}
# Checked in this order: safety first, then the most specific help.
INTENT_ORDER = ("identity", "distress", "human", "private", "password", "scan", "documents", "time", "decision")


# Asking for the next step, as opposed to a question the guide did not understand.
NEXT_WORDS = ("next", "what do i do", "what should i do", "what now", "help", "guide me", "start", "hello", "hi ",
              "आगे", "अब क्या", "क्या करना", "मदद", "नमस्ते", "selanjutnya", "apa yang harus", "bantu", "halo")


def intent_of(message: str) -> str:
    low = message.lower()
    for name in INTENT_ORDER:
        if any(k in low for k in KEYWORDS[name]):
            return name
    return "next"


def _lang(language: str) -> str:
    return language if language in LANGS else "en"


def _actions(state: dict[str, Any], lang: str, callback: bool) -> list[dict[str, str]]:
    out = []
    if state["action"] in ACTION_LABELS:
        out.append({"id": state["action"], "label": ACTION_LABELS[state["action"]][lang]})
    if callback:
        out.append({"id": "callback", "label": ACTION_LABELS["callback"][lang]})
    return out


# "What is EMI?" asks for a definition, not for the EMIs in the statement; "What is my income?" does not.
GLOSSARY_TERM = re.compile(r"\b(apr|kfs|key fact|emi|nach|cooling[- ]off|prepay\w*|penal\w*|grievance|udyam|gstin|"
                           r"cibil|aggregator|instalment|installment)\b", re.I)
DEFINITION = re.compile(r"^\s*(what is|what's|whats|what are|what does|meaning of|define|explain|apa itu|apa arti)\b"
                        r"|(क्या है|क्या होता|मतलब)\s*[?।]?\s*$", re.I)


def route(state: dict[str, Any], message: str, lang: str, case: Case | None) -> tuple[str, str, bool]:
    """The offline reply, what kind of answer it is, and whether to offer a call back.

    Kinds: step, unclear, redirect, identity, help:<topic>, statement:<action>, decision, glossary. The kind
    is what the question bank's tests check, so every suggested question provably gets the intended answer.
    """
    if not message.strip():
        return _step_text(state, lang), "step", False
    if detect(message):
        return SAFE_REDIRECT[lang], "redirect", False
    intent = intent_of(message)
    has_case = case is not None
    if intent == "identity":
        return HELP_TEXT["identity"][lang], "identity", False
    if intent == "distress":
        # Drop the help text's opening "Of course." so it follows the empathy line naturally.
        offer = re.split(r"(?<=[.।])\s", HELP_TEXT["callback" if has_case else "callback_later"][lang], maxsplit=1)[-1]
        return f"{EMPATHY[lang]} {offer}", "help:distress", has_case
    if intent == "human":
        return HELP_TEXT["callback" if has_case else "callback_later"][lang], "help:callback", has_case
    if DEFINITION.search(message) and GLOSSARY_TERM.search(message):
        glossary = _glossary(message)
        if glossary:
            return glossary, "glossary", False
    # Password and photo help use words no statement question does; ask them before reading the statement.
    if intent in ("password", "scan"):
        return HELP_TEXT[intent][lang], f"help:{intent}", False
    if case is not None and statement_qa.understands(case, message, lang):
        answer = statement_qa.StatementQA(case, lang, spoken=True)
        action = statement_qa.Parser(answer.start, answer.end).parse(message).action
        return answer.answer(message).text, f"statement:{action}", False
    if intent in HELP_TEXT:
        return HELP_TEXT[intent][lang], f"help:{intent}", False
    if intent == "decision" and case and case.decision and lang in case.explanations:
        draft = coach_offline(case.explanations[lang], message)
        return draft.reply, "decision", draft.offer_callback
    if GLOSSARY_QUESTION.search(message):
        glossary = _glossary(message)
        if glossary:
            return glossary, "glossary", False
    # Repeating the step to a question it did not understand sounds like the guide ignored the customer.
    if not _has_any(f" {message.lower()} ", NEXT_WORDS):
        return HELP_TEXT["unclear"][lang], "unclear", False
    return _step_text(state, lang), "step", False


def scripted(state: dict[str, Any], message: str, lang: str, case: Case | None) -> tuple[str, bool]:
    """The offline reply and whether to offer a call back. Also the fallback for every model draft."""
    reply, _kind, callback = route(state, message, lang, case)
    return reply, callback


def _glossary(message: str) -> str | None:
    from app.knowledge.search import search

    hits = search(message, audience="customer", k=1)
    return f"{hits[0]['title']}: {hits[0]['text']}" if hits else None


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def _step_text(state: dict[str, Any], lang: str) -> str:
    text = STEP_TEXT[state["step"]][lang].format(documents=state.get("documents", 0),
                                                 max_documents=state.get("max_documents", 3))
    if state.get("attention"):
        text = f"{state['attention']} {text}"
    return text


async def guide_reply(state: dict[str, Any], message: str, language: str, case: Case | None,
                      gateway: LLMGateway, meter: UsageMeter) -> dict[str, Any]:
    lang = _lang(language)
    reply, kind, callback = route(state, message, lang, case)
    author = "templates"
    # A model only rephrases free questions; greetings, safety cases and injections stay scripted.
    if gateway.enabled and message.strip() and not detect(message) and intent_of(message) in ("next", "documents", "time"):
        facts = {"language": lang, "journey": state, "approved_answer": reply, "user_message": message[:500]}
        try:
            draft = await gateway.structured(prompt_id="journey_guide", schema=CoachDraft, meter=meter,
                                             contents=[json.dumps(facts, ensure_ascii=False)])
            if _safe(draft.reply, json.dumps(facts, ensure_ascii=False)):
                reply, callback, author = draft.reply, callback or draft.offer_callback, gateway.provider
        except LLMUnavailable:
            pass
    return {"reply": reply, "kind": kind, "step": state["step"], "journey": state, "offer_callback": callback,
            "actions": _actions(state, lang, callback and case is not None), "author": author}


def _safe(reply: str, source: str) -> bool:
    if INTERNAL_TERMS.search(reply) or len(reply) > 600:
        return False
    allowed = set(NUMBER.findall(source))
    return all(n in allowed for n in NUMBER.findall(reply))

