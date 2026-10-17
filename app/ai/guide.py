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
        "en": "I'm {name}, an AI assistant that guides you through your loan application and answers questions about "
              "your statement. I'm not a person: a loan officer decides every application, and you can ask for a call "
              "back at any time.",
        "hi": "मैं {name} हूँ, एक AI सहायक जो आपके लोन आवेदन में मदद करता है और आपके स्टेटमेंट के बारे में सवालों के जवाब देता है। "
              "मैं कोई व्यक्ति नहीं हूँ: हर आवेदन का फ़ैसला लोन अधिकारी करते हैं, और आप कभी भी कॉल बैक माँग सकते हैं।",
        "id": "Saya {name}, asisten AI yang memandu pengajuan pinjaman Anda dan menjawab pertanyaan tentang rekening "
              "koran Anda. Saya bukan manusia: setiap pengajuan diputuskan oleh petugas pinjaman, dan Anda bisa meminta "
              "ditelepon kapan saja.",
    },
    "my_name": {
        "en": "Your application is in the name of {name}, for {business}.",
        "hi": "आपका आवेदन {name} के नाम पर है, {business} के लिए।",
        "id": "Pengajuan Anda atas nama {name}, untuk {business}.",
    },
    "my_name_unknown": {
        "en": "I don't have your details yet. They are filled in on the first step of the application.",
        "hi": "मेरे पास अभी आपकी जानकारी नहीं है। यह आवेदन के पहले चरण में भरी जाती है।",
        "id": "Saya belum memiliki data Anda. Data diisi pada langkah pertama pengajuan.",
    },
    "needs_application": {
        "en": "Please fill in the form first{demo} and press Save and continue, then ask me again. Once you upload "
              "your statement, I can also answer questions about your money.",
        "hi": "कृपया पहले फ़ॉर्म भरें{demo} और 'सेव करें और आगे बढ़ें' दबाएँ, फिर मुझसे दोबारा पूछें। स्टेटमेंट अपलोड होने के बाद "
              "मैं आपके पैसों से जुड़े सवालों के जवाब भी दे सकता हूँ।",
        "id": "Silakan isi formulir terlebih dahulu{demo} lalu tekan Simpan dan lanjutkan, kemudian tanyakan lagi. Setelah "
              "rekening koran diunggah, saya juga bisa menjawab pertanyaan tentang keuangan Anda.",
    },
    "needs_application_demo": {
        "en": " (or click Fill demo customer)",
        "hi": " (या 'Fill demo customer' दबाएँ)",
        "id": " (atau klik Fill demo customer)",
    },
    "unclear": {
        "en": "I can only help with your loan application and your bank statement. You can ask, for example, "
              "\"What do I do next?\", \"Where does my money go?\" or \"Which month had the lowest income?\"",
        "hi": "मैं सिर्फ़ आपके लोन आवेदन और बैंक स्टेटमेंट में मदद कर सकता हूँ। आप पूछ सकते हैं, जैसे \"अब मुझे क्या करना है?\", "
              "\"मेरा पैसा कहाँ जाता है?\" या \"किस महीने आमदनी सबसे कम रही?\"",
        "id": "Saya hanya bisa membantu soal pengajuan pinjaman dan rekening koran Anda. Anda bisa bertanya, misalnya "
              "\"Apa yang harus saya lakukan?\", \"Ke mana uang saya pergi?\" atau \"Bulan apa pendapatan terendah?\"",
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
    "new_application": {"en": "Start a new application", "hi": "नया आवेदन शुरू करें", "id": "Mulai pengajuan baru"},
}

# A new application, one field at a time. The page says which required field is still empty (or
# "consent" / "save" when only those are left), and the guide speaks the line for it.
FIELD_TEXT: dict[str, dict[str, str]] = {
    "full_name": {
        "en": "Let's start with your full name, exactly as it is printed on your PAN card.",
        "hi": "शुरुआत अपने पूरे नाम से करें, ठीक वैसा जैसा आपके PAN कार्ड पर छपा है।",
        "id": "Mari mulai dengan nama lengkap Anda, persis seperti tercetak di kartu PAN.",
    },
    "mobile": {
        "en": "Next, your ten-digit mobile number. A loan officer may call you on it.",
        "hi": "अब अपना दस अंकों का मोबाइल नंबर लिखें। लोन अधिकारी आपको इसी पर कॉल कर सकते हैं।",
        "id": "Berikutnya, nomor ponsel sepuluh digit Anda. Petugas pinjaman mungkin menelepon ke nomor ini.",
    },
    "pan": {
        "en": "Now your PAN. It has ten characters: five letters, four numbers, then one letter, as on your PAN card.",
        "hi": "अब अपना PAN लिखें। इसमें दस अक्षर होते हैं: पाँच अंग्रेज़ी अक्षर, चार अंक, फिर एक अक्षर, जैसा कार्ड पर है।",
        "id": "Sekarang PAN Anda. Ada sepuluh karakter: lima huruf, empat angka, lalu satu huruf, seperti di kartu PAN.",
    },
    "business_name": {
        "en": "Good. Now your business: write its name, then choose how it is registered and what it does.",
        "hi": "बढ़िया। अब अपने व्यवसाय का नाम लिखें, फिर चुनें कि वह किस रूप में पंजीकृत है और क्या काम करता है।",
        "id": "Bagus. Sekarang usaha Anda: tulis namanya, lalu pilih bentuk usaha dan bidangnya.",
    },
    "address_line": {
        "en": "Next, the business address: the shop or office number and the street.",
        "hi": "अब व्यवसाय का पता लिखें: दुकान या ऑफ़िस नंबर और गली या सड़क।",
        "id": "Berikutnya, alamat usaha: nomor toko atau kantor dan nama jalan.",
    },
    "city": {
        "en": "Now the city where your business runs.",
        "hi": "अब वह शहर लिखें जहाँ आपका व्यवसाय चलता है।",
        "id": "Sekarang kota tempat usaha Anda berjalan.",
    },
    "pincode": {
        "en": "Choose your state, then type the six-digit PIN code.",
        "hi": "अपना राज्य चुनें, फिर छह अंकों का पिन कोड लिखें।",
        "id": "Pilih negara bagian, lalu ketik kode PIN enam digit.",
    },
    "consent": {
        "en": "Almost done. Check the loan amount and how many months you want to repay, then read and tick the consent box.",
        "hi": "बस थोड़ा और। लोन की राशि और चुकाने के महीने देख लें, फिर सहमति वाला बॉक्स पढ़कर उस पर टिक करें।",
        "id": "Hampir selesai. Periksa jumlah pinjaman dan lama cicilan, lalu baca dan centang kotak persetujuan.",
    },
    "save": {
        "en": "Everything is filled in. Press Save and continue to bank statements.",
        "hi": "सब भर गया है। अब 'सेव करें और बैंक स्टेटमेंट पर आगे बढ़ें' दबाएँ।",
        "id": "Semua sudah terisi. Tekan Simpan dan lanjutkan ke rekening koran.",
    },
}
FIELDS = tuple(FIELD_TEXT)

GREETING: dict[str, str] = {
    "en": "Hello, I'm your application guide. I'll take you through it one step at a time.",
    "hi": "नमस्ते, मैं आपका आवेदन गाइड हूँ। मैं आपको एक-एक कदम करके आगे ले चलूँगा।",
    "id": "Halo, saya pemandu pengajuan Anda. Saya akan memandu Anda langkah demi langkah.",
}

# "My applications": a spoken summary of the person's own cases, in plain words.
LIST_STATUS: dict[str, dict[str, str]] = {
    "draft": {"en": "not submitted yet", "hi": "अभी जमा नहीं हुआ", "id": "belum dikirim"},
    "processing": {"en": "being read now", "hi": "अभी पढ़ा जा रहा है", "id": "sedang dibaca"},
    "attention": {"en": "needs your attention", "hi": "आपका ध्यान चाहिए", "id": "perlu perhatian Anda"},
    "review": {"en": "with a loan officer", "hi": "लोन अधिकारी के पास है", "id": "ada di petugas pinjaman"},
    "decided": {"en": "decision ready", "hi": "फ़ैसला आ गया है", "id": "keputusan sudah ada"},
}
LIST_TEXT: dict[str, dict[str, str]] = {
    "none": {
        "en": "You have no applications yet. Press New application and I'll guide you step by step.",
        "hi": "अभी आपका कोई आवेदन नहीं है। 'नया आवेदन' दबाएँ, मैं आपको एक-एक कदम करके बताऊँगा।",
        "id": "Anda belum punya pengajuan. Tekan Pengajuan baru dan saya akan memandu langkah demi langkah.",
    },
    "count": {
        "en": "You have {n} applications.", "hi": "आपके {n} आवेदन हैं।", "id": "Anda punya {n} pengajuan.",
    },
    "one": {
        "en": "You have one application.", "hi": "आपका एक आवेदन है।", "id": "Anda punya satu pengajuan.",
    },
    "close": {
        "en": "Open one and I'll tell you what to do next, or start a new application.",
        "hi": "किसी एक को खोलें, मैं बताऊँगा कि आगे क्या करना है, या नया आवेदन शुरू करें।",
        "id": "Buka salah satu dan saya akan memberi tahu langkah berikutnya, atau mulai pengajuan baru.",
    },
}
_LIST_GROUP = {"DRAFT": "draft", "PROCESSING": "processing", "NEEDS_MANUAL_PROCESSING": "attention",
               "READY_FOR_REVIEW": "review", "PENDING_SECOND_APPROVAL": "review"}

# The presenter on screen introduces themself by name.
PRESENTER_NAMES = {"female": {"en": "Sahayika", "hi": "सहायिका", "id": "Sahayika"},
                   "male": {"en": "Saarthi", "hi": "सारथी", "id": "Saarthi"}}

# Hindi first person is gendered. The guide's Hindi lines are written in the masculine; a female
# presenter speaks the same lines in the feminine.
_HI_FEMININE = (("सकता हूँ", "सकती हूँ"), ("समझता हूँ", "समझती हूँ"), ("पूछूँगा", "पूछूँगी"), ("बताऊँगा", "बताऊँगी"),
                ("चलूँगा", "चलूँगी"), ("करूँगा", "करूँगी"), ("रहा हूँ", "रही हूँ"),
                ("मदद करता है", "मदद करती है"), ("जवाब देता है", "जवाब देती है"), ("पाया।", "पाई।"))


def in_voice(text: str, lang: str, presenter: str) -> str:
    if lang == "hi" and presenter == "female":
        for male, female in _HI_FEMININE:
            text = text.replace(male, female)
    return text


KEYWORDS: dict[str, tuple[str, ...]] = {
    "identity": ("your name", "ur name", "who are you", "who r u", "are you a bot", "are you a robot", "are you human",
                 "are you real", "are you ai", "are you a person", "तुम्हारा नाम", "आपका नाम", "आप कौन", "तुम कौन",
                 "nama kamu", "nama anda", "siapa kamu", "siapa anda", "kamu robot", "tum kaun", "aap kaun",
                 "tumhara naam", "aapka naam"),
    "distress": ("worried", "stress", "hardship", "can't pay", "cannot pay", "scared", "परेशान", "चिंता", "मुश्किल",
                 "susah", "khawatir", "sulit", "takut", "pareshan", "chinta", "tension", "nahi chuka"),
    "human": ("human", "person", "officer", "call me", "call back", "callback", "talk to", "speak to", "agent",
              "अधिकारी", "बात", "कॉल", "इंसान", "petugas", "telepon", "manusia", "hubungi", "baat karni", "baat karna",
              "call karo", "call kar do", "kisi se baat"),
    "private": ("score", "band", "rating", "risk", "fraud", "neighbour", "neighbor", "someone else", "other customer",
                "other people", "स्कोर", "रेटिंग", "दूसरे", "पड़ोसी", "skor", "orang lain", "tetangga", "nasabah lain"),
    "password": ("password", "पासवर्ड", "kata sandi", "sandi", "locked", "won't open", "can't open"),
    "scan": ("scan", "photo", "picture", "camera", "फ़ोटो", "फोटो", "स्कैन", "foto", "pindai", "gambar"),
    "documents": ("document", "statement", "which file", "what file", "upload", "दस्तावेज़", "स्टेटमेंट", "अपलोड",
                  "dokumen", "rekening koran", "unggah", "berkas", "kaunse document", "kaun se document", "kya kya chahiye"),
    "time": ("how long", "when will", "how much time", "kab tak", "कब", "कितना समय", "कितने दिन", "berapa lama",
             "kapan", "kab milega", "kab milegi", "kab aayega", "kitna time", "kitna samay", "kitne din"),
    "decision": ("why", "reason", "improve", "better", "emi", "instalment", "installment", "interest", "repay",
                 "क्यों", "कारण", "सुधार", "किश्त", "ब्याज", "चुका", "kenapa", "alasan", "perbaiki", "cicilan", "bunga",
                 "kyun ", "kyon ", "byaj"),
}
# Checked in this order: safety first, then the most specific help.
INTENT_ORDER = ("identity", "distress", "human", "private", "password", "scan", "documents", "time", "decision")


# Asking for the next step, as opposed to a question the guide did not understand.
NEXT_WORDS = ("next", "what do i do", "what should i do", "what now", "help", "guide me", "start", "hello", "hi ",
              "आगे", "अब क्या", "क्या करना", "मदद", "नमस्ते", "selanjutnya", "apa yang harus", "bantu", "halo",
              "aage kya", "ab kya", "kya karna", "kya karu", "madad", "namaste")


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
                           r"cibil|aggregator|account aggregator|instalment|installment|statement password)\b", re.I)
# "What is my name?" is about the customer's own application; "What is your name?" is about the guide.
MY_NAME = re.compile(r"\bmy (full )?name\b|मेरा नाम|nama saya", re.I)
DEFINITION = re.compile(r"^\s*(what is|what's|whats|what are|what does|meaning of|define|explain|apa itu|apa arti)\b"
                        r"|(क्या है|क्या होता|मतलब)\s*[?।]?\s*$", re.I)


def route(state: dict[str, Any], message: str, lang: str, case: Case | None) -> tuple[str, str, bool]:
    """The offline reply, what kind of answer it is, and whether to offer a call back.

    Kinds: step, unclear, redirect, identity, help:<topic>, statement:<action>, decision, glossary. The kind
    is what the question bank's tests check, so every suggested question provably gets the intended answer.
    """
    if not message.strip():
        text = _step_text(state, lang)
        return (f"{GREETING[lang]} {text}" if state.get("greet") and state.get("field") else text), "step", False
    if detect(message):
        return SAFE_REDIRECT[lang], "redirect", False
    intent = intent_of(message)
    has_case = case is not None
    if MY_NAME.search(message) and not re.search(r"\byour name\b|आपका नाम|तुम्हारा नाम|nama (kamu|anda)", message, re.I):
        applicant = case.applicant if case is not None else None
        if applicant is not None:
            return HELP_TEXT["my_name"][lang].format(name=applicant.full_name, business=applicant.business_name), \
                "profile:name", False
        return HELP_TEXT["my_name_unknown"][lang], "profile:name", False
    if intent == "identity":
        names = PRESENTER_NAMES[state.get("presenter", "male")]
        return HELP_TEXT["identity"][lang].format(name=names.get(lang, names["en"])), "identity", False
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
    if case is None and statement_qa.is_statement_question(message):
        from app.config import get_settings

        settings = get_settings()
        demo = HELP_TEXT["needs_application_demo"][lang] if settings.demo_mode and not settings.is_prod else ""
        return HELP_TEXT["needs_application"][lang].format(demo=demo), "pending:application", False
    if case is not None and statement_qa.understands(case, message, lang):
        answer = statement_qa.StatementQA(case, lang, spoken=True)
        action = statement_qa.Parser(answer.start, answer.end).parse(message).action
        return answer.answer(message).text, f"statement:{action}", False
    if intent in HELP_TEXT:
        return HELP_TEXT[intent][lang], f"help:{intent}", False
    if intent == "decision" and case and case.decision and lang in case.explanations:
        draft = coach_offline(case.explanations[lang], message)
        return draft.reply, "decision", draft.offer_callback
    if GLOSSARY_QUESTION.search(message) and GLOSSARY_TERM.search(message):
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
    if state["step"] == "list":
        return list_text(state.get("cases", []), lang)
    if state["step"] == "details" and state.get("field") in FIELD_TEXT:
        return FIELD_TEXT[state["field"]][lang]
    text = STEP_TEXT[state["step"]][lang].format(documents=state.get("documents", 0),
                                                 max_documents=state.get("max_documents", 3))
    if state.get("attention"):
        text = f"{state['attention']} {text}"
    return text


def list_text(rows: list[dict[str, Any]], lang: str) -> str:
    """A spoken summary of the person's own applications, newest first (at most five are read out)."""
    if not rows:
        return LIST_TEXT["none"][lang]
    parts = [LIST_TEXT["one"][lang] if len(rows) == 1 else LIST_TEXT["count"][lang].format(n=len(rows))]
    for row in rows[:5]:
        group = _LIST_GROUP.get(row.get("status", ""), "decided")
        parts.append(f"{row.get('business_name') or '-'}: {LIST_STATUS[group][lang]}.")
    parts.append(LIST_TEXT["close"][lang])
    return " ".join(parts)


def mood_of(state: dict[str, Any], message: str, kind: str) -> str:
    """How the presenter's face should look while saying the reply. Expression only, never content."""
    if kind == "help:distress" or (kind == "decision" and state.get("outcome") == "declined"):
        return "empathetic"
    if kind in ("unclear", "redirect", "help:private"):
        return "polite"
    if not message.strip():
        if state["step"] in ("accept_terms", "done") or state.get("field") == "save":
            return "happy"
        if state.get("outcome") == "declined":
            return "empathetic"
        if state.get("greet"):
            return "happy"
    return "warm"


async def guide_reply(state: dict[str, Any], message: str, language: str, case: Case | None,
                      gateway: LLMGateway, meter: UsageMeter, presenter: str = "female") -> dict[str, Any]:
    from app.config import get_settings

    lang = _lang(language)
    state = state | {"presenter": presenter}
    reply, kind, callback = route(state, message, lang, case)
    author = "templates"
    if case is not None and case.transactions and (
            kind == "unclear" or (kind.startswith("statement:") and statement_qa.guessed(case, message))):
        result = await statement_qa.answer_with_ai(case, message, lang, gateway, meter, spoken=True)
        if result.understood and result.action:
            reply, kind, author = result.text, f"statement:{result.action}", result.author
    # A model only rephrases free questions about the application; greetings, safety cases, injections
    # and anything off topic stay scripted.
    if (get_settings().ai_rephrase and gateway.enabled and message.strip() and not detect(message)
            and kind in ("step", "help:documents", "help:time")):
        facts = {"language": lang, "journey": {k: v for k, v in state.items() if k not in ("cases", "greet", "presenter")},
                 "approved_answer": reply, "user_message": message[:500]}
        try:
            draft = await gateway.structured(prompt_id="journey_guide", schema=CoachDraft, meter=meter,
                                             contents=[json.dumps(facts, ensure_ascii=False)])
            if _safe(draft.reply, json.dumps(facts, ensure_ascii=False)):
                reply, callback, author = draft.reply, callback or draft.offer_callback, gateway.provider
        except LLMUnavailable:
            pass
    actions = _actions(state, lang, callback and case is not None)
    if state["step"] == "list":
        actions = [{"id": "new_application", "label": ACTION_LABELS["new_application"][lang]}]
    public_state = {k: v for k, v in state.items() if k not in ("cases", "greet", "presenter")}
    return {"reply": in_voice(reply, lang, presenter), "kind": kind, "step": state["step"], "journey": public_state,
            "offer_callback": callback, "actions": actions, "author": author, "mood": mood_of(state, message, kind),
            "field": state.get("field")}


def _safe(reply: str, source: str) -> bool:
    if INTERNAL_TERMS.search(reply) or len(reply) > 600:
        return False
    allowed = set(NUMBER.findall(source))
    return all(n in allowed for n in NUMBER.findall(reply))

