"""Travel-domain gate (docs/voice-and-domain-controls.md): a CONSERVATIVE, deterministic
pre-filter for English and French.

It is deliberately NOT an allowlist of travel words. In Phase 0 the model never writes reply
text: every reply is server-authored, FRAME only emits a patch, an intent and typed search
arguments, and discovery runs only through the server's tool allowlist. A message the gate
passes on that is not really an outing therefore cannot become a general-purpose answer; at
worst FRAME returns intent OTHER and the user gets one fixed, travel-focused clarification.
The gate only has to be sure about what it REFUSES:

1. SAFETY: emergency and self-harm language (EN/FR) -> the fixed safety response, no model.
2. APP_HELP: questions about this app's controls, privacy, account and usage -> fixed handling.
3. OUT_OF_SCOPE: clearly off-topic requests (code, homework, essays, translation, maths,
   medical/legal/financial advice, trivia, jokes and stories, roleplay, questions about the
   model or its prompt, hacking, other products) that contain NO travel-seeking content ->
   the fixed redirect, no model call.
4. A clearly off-topic part next to a travel part (MIXED) is NOT refused: it continues as
   TRAVEL and FRAME extracts only the outing (the model cannot answer the rest).
5. Everything else, however short or vague, is TRAVEL.

Matching runs on a folded view of the text: NFKC, hidden/format characters removed, common
Cyrillic/Greek look-alikes mapped to Latin, case-folded, accents stripped, apostrophes and
hyphens turned into spaces. Off-topic and safety patterns also run on a view that undoes simple
leetspeak and spaced-out letters. Patterns are word-bounded phrases with intent verbs, never
bare words that appear in travel talk.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .domain import DomainClass, PrincipalKind

OUT_OF_SCOPE_REPLY = (
    "I can help plan trips and outings. Tell me where you want to go, what you want "
    "to do, or when you need to arrive or return."
)
SAFETY_REPLY = "If there is immediate danger, contact local emergency services. I can help with travel planning once you are safe."


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


# --- normalisation -------------------------------------------------------------------------
_HOMOGLYPHS = str.maketrans({
    # Cyrillic
    "а": "a", "в": "b", "с": "c", "е": "e", "н": "h", "і": "i", "ј": "j", "к": "k",
    "м": "m", "о": "o", "р": "p", "ѕ": "s", "т": "t", "у": "y", "х": "x", "ү": "y",
    "ԁ": "d", "ɡ": "g", "ӏ": "l", "ո": "n", "ս": "u",
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "І": "I", "Ј": "J", "К": "K",
    "М": "M", "О": "O", "Р": "P", "Ѕ": "S", "Т": "T", "У": "Y", "Х": "X",
    # Greek
    "α": "a", "ο": "o", "ρ": "p", "ν": "v", "ι": "i", "κ": "k", "τ": "t", "υ": "u",
    "χ": "x", "ε": "e", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I",
    "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    # a few more look-alikes
    "ǀ": "l", "ɑ": "a", "ᴀ": "a", "ᴄ": "c", "ᴇ": "e", "ᴏ": "o",
})
_LIGATURES = str.maketrans({"œ": "oe", "æ": "ae", "ø": "o", "ł": "l", "đ": "d", "ı": "i"})
_SEPARATORS = re.compile(r"['’`´\-‐‑‒–—_]")
_SPACES = re.compile(r"\s+")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s",
                       "!": "i"})
_MIXED_TOKEN = re.compile(r"\S*[^\W\d_]\S*\d\S*|\S*\d\S*[^\W\d_]\S*|\S*[a-z][@$]\S*|\S*[@$][a-z]\S*")
_SPACED_LETTERS = re.compile(r"(?<![^\W_])(?:[^\W_][\s.*_]){3,}[^\W_](?![^\W_])")


def _fold(text: str) -> str:
    """The plain folded view (see module docstring)."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(
        " " if unicodedata.category(c) in ("Cc", "Zs", "Zl", "Zp") else c
        for c in text if unicodedata.category(c) != "Cf"
    )
    text = text.translate(_HOMOGLYPHS)
    text = unicodedata.normalize("NFD", text.casefold().translate(_LIGATURES))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = _SEPARATORS.sub(" ", unicodedata.normalize("NFC", text))
    return _SPACES.sub(" ", text).strip()


def _leet(plain: str) -> str:
    """Undo digit/symbol substitutions inside words that also hold letters, and join letters
    that were spaced out ("k i l l"). Numbers on their own are left alone."""
    out = _MIXED_TOKEN.sub(lambda m: m.group(0).translate(_LEET), plain)
    out = _SPACED_LETTERS.sub(lambda m: re.sub(r"[\s.*_]", "", m.group(0)), out)
    return out


def _views(text: str) -> tuple[str, ...]:
    plain = _fold(text)
    other = _leet(plain)
    return (plain,) if other == plain else (plain, other)


def _any(patterns: list[re.Pattern[str]], views: tuple[str, ...]) -> bool:
    return any(p.search(v) for p in patterns for v in views)


def _compile(*sources: str) -> list[re.Pattern[str]]:
    return [re.compile(s) for s in sources]


def _w(n: int, m: int | None = None) -> str:
    """Between n and m (lazy) filler words, each preceded by a non-word gap."""
    return rf"(?:\W+\w+){{{n},{m if m is not None else n}}}?\W+"


# --- SAFETY (emergency and self-harm; EN/FR) -----------------------------------------------
_SAFETY = _compile(
    r"\b(?:kill(?:ing)? my ?self|end(?:ing)? my life|end it all|take my own life|taking my own life|"
    r"want(?:s)? to die|wanna die|don t want to (?:be alive|live|be here|go on)|"
    r"better off dead|no reason to live|suicid\w*|self ?harm\w*|hurt(?:ing)? my ?self|"
    r"cut(?:ting)? my ?self|harm(?:ing)? my ?self|overdos\w+|too many (?:pills|tablets)|"
    r"take (?:all )?my pills)\b",
    r"\b(?:me tuer|me suicider|me donner la mort|mettre fin a (?:mes jours|ma vie)|veux mourir|"
    r"envie de mourir|veux en finir|envie d en finir|en finir avec (?:la vie|tout)|"
    r"finir (?:mes jours|ma vie)|me faire (?:du )?mal|me blesser|me couper les veines|"
    r"automutil\w+|auto mutil\w+|ne veux plus vivre|plus envie de vivre|"
    r"ne veux plus etre en vie|idees noires|trop de (?:pilules|comprimes|medicaments)|"
    r"m empoisonner)\b",
    # emergency vocabulary, phrase-based: "emergency exit" and "en cas d urgence" are travel talk
    r"\b(?:this is|it s|its|there s|there is|have|having|had|an?|medical|real|serious|major)\s+"
    r"(?:(?:an|a|real|serious|medical|major)\s+)?emergency\b"
    r"(?!\s+(?:exits?|stairs?|stairways?|routes?|lane|brakes?|plan|kit|contacts?|numbers?|"
    r"prepared\w*|supplies|shelter|signs?|lighting|doors?|only|parking))",
    r"\b(?:medical emergency|emergency (?:services|help|room|number)|call (?:an )?ambulance|"
    r"need (?:an )?ambulance|need help now|help me now)\b",
    r"\b(?:c est|il y a|j ai) une urgence\b|\burgence medicale\b|\b(?:aux|les|des) urgences\b",
    r"\b(?:call|dial|calling) (?:911|112)\b|\bappel(?:le|ez|er)? (?:le )?(?:911|112)\b|\bcomposer le (?:911|112)\b",
    r"\bheart attack\b|\bcrise cardiaque\b|\b(?:can t|cant|cannot|can not|unable to|couldn t) breathe\b|"
    r"\b(?:peux|peut|arrive) (?:pas|plus) a respirer\b|\bne peux (?:pas|plus) respirer\b|"
    r"\bdifficulte a respirer\b|\bchest pains?\b|\bdouleurs? (?:a|dans) la poitrine\b|"
    r"\bdouleurs? thoraciques?\b|\bhaving a (?:stroke|seizure)\b|\bfais une (?:crise|avc)\b",
    r"\b(?:unconscious|not breathing|stopped breathing|isn t breathing|passed out and)\b|"
    r"\binconscient\w*\b|\bne respire(?:nt)? (?:plus|pas)\b|\b(?:choking|choked)\b|\betouff\w+\b|"
    r"\bbleeding (?:badly|heavily|a lot|out|profusely)\b|\bsaigne(?:nt)? (?:beaucoup|abondamment)\b|"
    r"\bhemorragie\b",
    r"\b(?:i am|i m|im|we are|we re|je suis|nous sommes|on est)\s+(?:in (?:real |serious |great )?danger|"
    r"en danger)\b",
    r"\b(?:someone|somebody|a man|a guy|he|she|they|my (?:ex|husband|wife|partner|boyfriend|girlfriend))\b"
    + _w(0, 3) + r"(?:following|chasing|stalking|threatening|attacking|hitting|beating|assaulting|"
    r"strangling|choking|hurting|raping|abusing) (?:me|us)\b",
    r"\b(?:quelqu un|il|elle|un homme|ils|elles|mon (?:ex|mari|conjoint|copain)|ma (?:femme|conjointe|copine))\s+"
    r"(?:me|nous)\s+(?:suit|suivent|poursuit|menace|attaque|frappe|agresse|harcele|bat|etrangle|viole|"
    r"fait peur)\b",
    r"\b(?:there s|there is|il y a)\s+(?:a |an |un |une )?(?:fire|incendie)\b"
    r"(?!\s*(?:pit|pits|place|side|works|station|hall|trail|escape|exit|show|dance|d artifice|de camp))",
    r"\b(?:on fire|au feu|en feu|building burning|house burning)\b",
    r"\b(?:my|mon|ma|mes)\s+(?:child|kid|baby|son|daughter|mom|mother|dad|father|friend|husband|wife|"
    r"enfant|bebe|fils|fille|mere|pere|ami|amie|mari|femme)\b" + _w(0, 3)
    + r"(?:is|are|has|est|a|ont)\s+(?:dying|unconscious|choking|bleeding|having a (?:seizure|stroke|"
    r"heart attack)|overdos\w+|drowning|en train de (?:mourir|s etouffer|se noyer)|se noie|mourant)\b",
)


# --- APP_HELP ------------------------------------------------------------------------------
_APP_OBJECTS = (
    r"account|accounts|compte|comptes|data|donnees|privacy|vie privee|usage|allowance|quota|history|"
    r"historique|settings|parametres|permissions?|autorisations?|subscription|abonnement|password|"
    r"mot de passe|notifications?|microphone|micro|location history|position|(?:saved )?trips|"
    r"voyages(?: enregistres)?|conversations?|chats?|messages"
)
_APP_ACTIONS = (
    r"delete|remove|cancel|close|export|download|clear|erase|reset|wipe|supprime\w*|efface\w*|"
    r"annule\w*|ferme\w*|exporte\w*|telecharge\w*|reinitialise\w*|vide\w*"
)
_APP_HELP = _compile(
    r"\b(?:this|the|cette|l|mon|notre)\s+(?:app|application|appli|assistant|service|site)\b"
    r"(?:.{0,40}?\b(?:privacy|data|work|works|do|does|handle|store|use|track|listen|permission|"
    r"fonctionne|donnees|vie privee|gere|stocke|utilise|ecoute|suit|autorisation)\w*)",
    r"\b(?:does|do|est ce que|est ce qu)\s*(?:the|this|l|cette)\s+(?:app|application|appli)\b",
    r"\bhow (?:does|do) (?:this|the|you|it) (?:app |application |assistant |service )?work\b"
    r"(?!.{0,30}\b(?:ferry|transit|subway|ttc|go train|bus|streetcar|elevator|patio)\b)",
    r"^\W*(?:how does (?:it|this) work|comment (?:ca|cela) (?:fonctionne|marche)|what can you do|"
    r"que sais tu faire|que peux tu faire|qu est ce que tu peux faire)\W*$",
    r"\bcomment fonctionne (?:cette|l|le bouton)\b",
    r"\b(?:my|mon|ma|mes|your|votre|vos)\s+(?:\w+\s+)?(?:" + _APP_OBJECTS + r")\b",
    r"\b(?:" + _APP_ACTIONS + r")\s+(?:\w+\s+){0,2}(?:" + _APP_OBJECTS + r")\b",
    r"\b(?:turn|switch|disable|enable|allow|block|stop|desactive\w*|active\w*|coupe\w*|autorise\w*|"
    r"bloque\w*)\b" + _w(0, 3) + r"(?:microphone|mic|location|geolocation|position|notifications?|voice|"
    r"voix|micro|localisation)\b",
    r"\b(?:do|does|are|is)\s+you\s+(?:store|keep|save|sell|share|track|collect|record|remember|log|use|listen)\b",
    r"\b(?:store|keep|save|sell|share|track|collect|record|remember|log|use)\s+(?:my|our)\s+"
    r"(?:data|location|voice|audio|conversations?|messages?|information)\b",
    r"\b(?:partagez|gardez|stockez|suivez|enregistrez|collectez|conservez|vendez|ecoutez|sauvegardez|"
    r"utilisez)\s+vous\b",
    r"\bwhat (?:data|information|permissions?)\s+(?:do|does|are|is)\b|\bquelles? (?:donnees|autorisations?|permissions?)\b",
    r"\bhow many (?:turns|requests|messages|searches|questions|trips) (?:do i have|have i|are left|left|can i)\b|"
    r"\bcombien de (?:tours|demandes|messages|recherches|questions|voyages) (?:me reste|puis je|ai je|restent|il me reste)\b",
    r"\bprivacy(?: policy)?\b|\bconfidentialite\b|\bvie privee\b|\bterms of (?:service|use)\b|"
    r"\bconditions d utilisation\b|\busage limits?\b|\blimites? d utilisation\b|\bcookies?\b",
    r"\b(?:sign (?:in|up|out)|log (?:in|out)|login|logout|link my|me connecter|me deconnecter|"
    r"creer un compte|lier mon|lier ma)\b",
    r"\bchange (?:my |the |your )?(?:app )?(?:language|langue)\b|\bchanger (?:la |de |ma )?langue\b",
    r"\b(?:push to talk|hold to (?:talk|record)|appuyer pour parler|bouton pour parler)\b",
)


# --- travel evidence (rescues an off-topic hit: the message also asks for an outing) ---------
_PLACE_NOUNS = (
    r"restaurants?|resto|restos|cafes?|coffee shops?|coffee|bars?|pubs?|bakery|bakeries|brunch|lunch|"
    r"dinner|breakfast|pizzas?|pizzeria|ramen|sushi|tacos|burgers?|bistro|diner|patisserie|"
    r"boulangerie|dejeuner|souper|brasserie|microbrasserie|brewery|breweries|terrasse|patio|patios|"
    r"parks?|parcs?|trails?|sentier|beach|plage|museums?|musees?|galleries|gallery|galerie|"
    r"theatres?|theaters?|cinema|movie theatres?|markets?|marches?|malls?|hotels?|zoo|gardens?|jardin|"
    r"playground|library|bibliotheque|bibliotheques|spa|bookstores?|librairie|shops?|stores?|boutique|"
    r"magasin|gelato|ice cream|creme glacee|glace|spots?|places?|endroits?|lieux?|activity|activities|"
    r"activite|activites|events?|evenements?|spectacles?|concerts?|festivals?|hikes?|walks?|"
    r"randonnees?|promenades?|visites?|tours?|attractions?|rides?|table|tables|"
    r"things to do|choses a faire"
)
_DET = r"(?:a|an|the|some|any|un|une|le|la|les|des|du|de la|au|aux|d un|d une|mon|ma|nos|notre|our|my)"
_TRIP_NOUNS = r"trip|outing|itinerary|itineraire|day ?trip|getaway|excursion|escapade|sortie|voyage|randonnee"
_TRAVEL_EVIDENCE = _compile(
    r"\b" + _DET + r"\s+(?:\w+\s+){0,2}(?:" + _PLACE_NOUNS + r")\b",
    r"\b" + _DET + r"\s+(?:\w+\s+){0,2}(?:" + _TRIP_NOUNS + r")\b",
    r"\b(?:things to do|something to do|something fun|something for the kids|quelque chose a faire|"
    r"choses a faire|activites? pour)\b",
    r"\b(?:where|ou)\b(?:\W+\w+){0,4}?\W+(?:eat|go|hang|walk|stay|sit|brunch|grab|take|visit|hike|shop|"
    r"rent|meet|dine|manger|aller|sortir|boire|marcher|louer|emmener|dejeuner|diner|souper|prendre|"
    r"rencontrer)\b",
    r"\b(?:near|nearby|near me|close to|around here|in the area|pres|pres de|pres d|proximite|"
    r"autour de|dans le coin|pas loin|a cote)\b",
    r"\b(?:take me to|bring me to|go(?:ing)? to|head(?:ing)? to|drive to|walk to|get to|how (?:do i|to) get to|"
    r"emmene moi|amene moi|conduis moi|veux aller|voudrais aller|aller (?:a|au|aux|en)|"
    r"visit(?:er|ing)? (?:the|a|le|la|les))\b",
    r"\b(?:is|are|est|sont)\b.{0,40}?\b(?:open|ouvert\w*|running|closed|ferme\w*)\b|"
    r"\b(?:open|ouvert\w*)\s+(?:now|late|tonight|today|until|maintenant|tard|ce soir|aujourd hui|jusqu)\b",
    r"\b(?:i m|i am|we are|j ai|on a|nous avons|we have|i have)\s+(?:\w+\s+){0,2}"
    r"(?:hungry|faim|free time|hours? (?:free|before|to kill)|heures? (?:libres|avant))\b",
    r"\bfind\s+(?:me\s+)?(?:\w+\s+){0,3}(?:" + _PLACE_NOUNS + r")\b|"
    r"\b(?:trouve|cherche|trouvez|cherchez)\w*(?:\s+moi)?\s+(?:\w+\s+){0,3}(?:" + _PLACE_NOUNS + r")\b",
)


# --- OUT_OF_SCOPE (clearly off-topic; EN/FR) -----------------------------------------------
_CODE_VERBS = (
    r"write|code|debug|fix|refactor|implement|generate|build|develop|ecris|ecrire|ecrivez|coder|codez|"
    r"corrige|corrigez|corriger|deboguer|debogue|genere|developpe|developper|construis|cree|creer|optimise"
)
_CODE_NOUNS = (
    r"python|javascript|typescript|java|sql|html|css|rust|golang|bash|powershell|regex|php|kotlin|"
    r"c\+\+|script|function|fonction|code|bug|query|requete|website|site web|snippet|algorithm|algorithme"
)
_LANGS = r"python|javascript|typescript|java|sql|html|css|regex|react|rust|golang|bash|powershell|c\+\+|php|kotlin"
_WRITE_TARGETS = (
    r"essays?|essai|dissertation|homework|devoirs?|thesis|these de \w+|poems?|poeme|poemes|poesie|"
    r"stor(?:y|ies)|histoire|conte|songs?|chanson|lyrics|paroles|haikus?|limericks?|jokes?|blagues?|"
    r"novel|roman|screenplay|speech|discours|cover letter|lettre de motivation|resume|cv|emails?|"
    r"courriels?|e mails?|blog post|article de blog|paragraph|paragraphe|report|rapport|letter|lettre|"
    r"toast|slogan|tweet|press release|communique de presse"
)
_MATH_WORDS = (
    r"equation|integral|integrale|derivative|derivee|polynomial|matrix|matrice|sqrt|formula|formule|"
    r"theorem|theoreme|probabilit\w*|algebra|algebre|inequality|inequation|fraction"
)
_ARITH = r"\d[\d.,]*\s*[+*×÷^=]\s*\d"
_MEDICINES = (
    r"ibuprofen\w*|tylenol|advil|aspirin\w*|aspirine|paracetamol|acetaminophen|medic\w+|pills?|"
    r"comprimes?|antibiotic\w*|antibiotique\w*|\bmg\b|insulin\w*|insuline"
)
_SYMPTOMS = (
    r"headaches?|migraines?|rash|fever|fievre|cough|toux|mal de (?:tete|ventre|dos|gorge)|"
    r"stomach ?ache|nausea|nausee"
)
_OFF_TOPIC = _compile(
    # code
    rf"\b(?:{_CODE_VERBS})\b{_w(0, 6)}(?:{_CODE_NOUNS})\b",
    rf"\b(?:corrige|corrigez|debug|fix)\b{_w(0, 2)}(?:program|programme|script|fonction|function|code)\b",
    rf"\b(?:{_LANGS})\s+(?:code|script|function|program|snippet|class|bug|error|exception|programme|"
    r"fonction|query|tutorial|homework|devoirs?|assignment|project|projet)\b",
    r"\b(?:stack ?trace|traceback|syntax error|erreur de syntaxe|compile error|unit tests?|"
    r"tests? unitaires?|pull request|git (?:commit|merge|rebase|push|clone)|npm install|pip install|"
    r"leetcode|hackerrank|stack overflow|null pointer|segfault|segmentation fault)\b",
    # homework, essays, creative writing, summaries, translation
    rf"\b(?:write|draft|compose|ecris|ecrire|ecrivez|redige|rediger|redigez|composer|generate|genere)\b"
    rf"{_w(0, 5)}(?:{_WRITE_TARGETS})\b",
    r"\b(?:do|finish|complete|help (?:me )?with|solve|answer|fais|fait|faire|termine|termines|aide moi (?:a|avec|pour)|"
    r"aide (?:a|avec|pour)|resous|resoudre)\b" + _w(0, 4)
    + r"(?:homework|devoirs?|assignments?|exam|examen|quiz|exercices?|problem set|worksheet|essay|dissertation)\b",
    r"\b(?:summari[sz]e|tl ?dr|resume (?:moi|ce|cet|cette|le|l|les|ces)|resumer|paraphrase\w*|"
    r"reformule\w*|proofread|rewrite|reecris\w*|translate|traduis\w*|traduire|traduction)\b",
    # maths
    rf"\b(?:solve|calculate|compute|evaluate|simplify|differentiate|integrate|resous|resoudre|calcule|"
    rf"calculez|calculer|derive|integre|simplifie|factorise|demontre)\b{_w(0, 4)}(?:{_MATH_WORDS})\b",
    rf"\b(?:solve|calculate|compute|evaluate|resous|resoudre|calcule|calculez|calculer|simplify|simplifie)\b"
    rf".{{0,40}}?{_ARITH}",
    r"\b(?:what is|what s|whats|how much is|quel est|combien (?:font|fait|vaut))\b\W+\d[\d.,]*\s*"
    r"(?:[+*×÷^]|times|plus|minus|divided by|fois|moins|divise par|multiplie par)\s*\d",
    r"\b(?:square root|racine carree|derivative of|derivee de|integral of|integrale de|prime numbers?|"
    r"nombres? premiers?|pythagor\w+|quadratic|quadratique|logarithm\w*|trigonometr\w+|calculus|"
    r"calcul differentiel)\b",
    # medical, legal, financial
    r"\bdiagnos(?:e|es|is|ed|ing|tic|tique|tiquer|tiquez)\b",
    rf"\b(?:how much|how many|combien|dose of|dose de|can i take|puis je prendre|dois je prendre|"
    rf"should i take|can i mix|puis je melanger|what dose|quelle dose)\b.{{0,30}}?(?:{_MEDICINES})",
    r"\b(?:what should i take for|what to take for|quoi prendre|que dois je prendre pour|que prendre pour|"
    r"what (?:medicine|medication|pill) (?:should|can|do))\b",
    r"\b(?:my|mes|mon|ma|these|ces)\s+(?:symptoms?|symptomes?)\b|\b(?:side effects?|effets secondaires|"
    r"dosage|posologie)\b",
    rf"\b(?:cure|treat|treatment|remedy|remedies|soigner|guerir|traitement|remede|remedes|cause)\b.{{0,25}}?"
    rf"\b(?:{_SYMPTOMS})\b",
    r"\b(?:legal advice|conseils? juridiques?|can i sue|puis je poursuivre|sue my|sue the|poursuivre mon|"
    r"poursuivre ma|is it legal|est il legal|est ce legal|my lawyer|mon avocat|custody|garde des enfants|"
    r"divorce|file my taxes|file taxes|tax return|declaration d impots?|declaration de revenus|"
    r"lawsuit|poursuite judiciaire|immigration law|visa application)\b",
    r"\b(?:draft|write)\s+(?:a |my |an )?(?:contract|will|lease)\b|\bredige\w*\s+(?:un |mon )?(?:contrat|testament|bail)\b",
    r"\b(?:bitcoin|ethereum|dogecoin|crypto(?:currency|currencies|monnaie|monnaies)?|etf|mortgage|"
    r"hypotheque|rrsp|reer|tfsa|celi|forex|dividends?|index fund|fonds indiciel|stock market|bourse|"
    r"financial advice|conseils? financiers?|retirement (?:plan|savings)|portfolio)\b",
    r"\binvest(?:ing|ir|issement)?\s+(?:in|dans|my|mon|mes)\b",
    # general knowledge
    r"\b(?:who|qui)\s+(?:is|was|etait|est)\s+(?:the |le |la )?(?:president|prime minister|premier ministre|"
    r"king|queen|roi|reine|founder|fondateur|ceo|pdg|author|auteur|inventor|inventeur|first|premier|"
    r"greatest|plus grand|richest|plus riche|best (?:player|actor|singer))\b",
    r"\b(?:who|qui a)\s+(?:invented|wrote|discovered|painted|founded|composed|directed|invente|ecrit|"
    r"decouvert|peint|fonde|compose|realise|gagne)\b",
    r"\bwhat is the (?:capital|population|meaning|definition|speed of|boiling|melting|atomic|gdp|currency)\b|"
    r"\bquelle est la (?:capitale|population|signification|definition|vitesse|monnaie)\b",
    r"\bhow many (?:planets|countries|continents|moons)\b|\bwhy is the (?:sky|sea|ocean|grass)\b|"
    r"\bpourquoi le (?:ciel|soleil)\b|\bpourquoi la (?:mer|lune)\b",
    r"\bwhen (?:was|did|were)\b.{0,40}?\b(?:born|invented|founded|die|died|war|written|discovered)\b",
    r"\b(?:explain|explique\w*|expliquer)\b" + _w(0, 3)
    + r"(?:quantum|quantique|relativity|relativite|photosynthesis|photosynthese|blockchain|inflation|"
    r"gravity|gravite|machine learning|neural|intrication|entanglement|evolution|climate change|"
    r"changement climatique|economy|economie|politics|politique|war|guerre|religion|philosophy|"
    r"philosophie|dna|adn|black holes?|trous? noirs?|big bang)\b",
    # jokes, stories, songs
    r"\b(?:tell|say|crack|give|raconte|dis|donne|fais|conte)\b" + _w(0, 4)
    + r"(?:jokes?|riddles?|stor(?:y|ies)|fairy tales?|blagues?|devinettes?|histoires?|contes?)\b",
    r"\b(?:knock knock|toc toc|bedtime story|histoire avant de dormir)\b",
    r"\b(?:sing|chante\w*)\b" + _w(0, 3) + r"(?:song|chanson|lullaby|berceuse)\b",
    # roleplay
    r"\b(?:pretend|act as|act like|roleplay|role play|you are now|you re now|from now on you are|"
    r"imagine you are|play the role|simulate being|joue le role|fais semblant|agis comme|tu es maintenant|"
    r"vous etes maintenant|fais comme si|imagine que tu es|incarne\w*|prends le role|a partir de maintenant tu es|"
    r"dorenavant tu es)\b",
    r"\b(?:be my|sois mon|sois ma|act as my|agis comme mon)\b" + _w(0, 2)
    + r"(?:therapist|psychologist|therapeute|psychologue|psy|doctor|medecin|lawyer|avocat|teacher|"
    r"professeur|girlfriend|boyfriend|copine|copain)\b",
    r"\b(?:therapist|psychologist|psychologue|therapeute|psychotherap\w+|mental health advice)\b",
    r"\b(?:i feel|i am feeling|i m feeling|i m so|je me sens|je suis (?:tellement|si|tres))\b" + _w(0, 2)
    + r"(?:depressed|anxious|lonely|worthless|hopeless|empty|overwhelmed|deprime\w*|anxieux|anxieuse|seul|"
    r"seule|inutile|vide|submerge\w*)\b",
    # the model, its prompt, injection
    r"\b(?:your|ton|ta|tes|votre|vos)\s+(?:\w+\s+)?(?:system prompt|prompt|instructions?|rules|regles|"
    r"consignes?|directives?|programming|programmation|training|entrainement|developer|developpeur|"
    r"creator|createur|model|modele|guidelines|configuration)\b",
    r"\b(?:system prompt|prompt systeme|developer message|jailbreak|do anything now|dan mode|"
    r"mode developpeur|developer mode|prompt injection|hidden prompt|secret prompt)\b",
    r"\b(?:initial|original|hidden|secret|cachees?|initiales?)\s+(?:prompt|instructions?|consignes?)\b",
    r"\b(?:are you|es tu|etes vous|tu es|vous etes)\s+(?:(?:an?|une?|really|vraiment|actually)\s+)?"
    r"(?:chatgpt|gpt|claude|gemini|openai|bard|llama|copilot|ai|bot|robot|human|real|sentient|alive|"
    r"conscious|ia|humain|reel|vivant|conscient|machine)\b",
    r"\b(?:which|what|quel|quelle)\b" + _w(0, 2) + r"(?:model|llm|ai|modele|ia)\b" + _w(0, 2)
    + r"(?:are you|do you use|is this|utilises tu|es tu|utilisez vous)\b",
    r"\bwho (?:made|created|built|trained|programmed) you\b|\bqui t a (?:cree|fait|programme|developpe|entraine)\b",
    r"\b(?:ignore|disregard|forget|override|bypass|disable|oublie|oubliez|contourne\w*|desactive\w*|"
    r"ignorer|ignorez|ignores)\b" + _w(0, 4)
    + r"(?:instructions?|rules?|regles?|consignes?|prompts?|directives?|guidelines?|restrictions?|"
    r"filters?|filtres?|guardrails?|safeguards?|limits?|limites?)\b",
    # hacking and harmful how-tos
    r"\bhack(?:ing)?\s+(?:into|in to|someone|somebody|a|the|my|his|her|their|wifi|wi fi|accounts?)\b",
    r"\b(?:hackers?|malware|ransomware|keylogger|ddos|phishing|sql injection|xss|brute force|"
    r"exploit (?:a|the|this|that)|crack(?:ing)? (?:a |the |this |that |my |his |her |someone s )?"
    r"(?:password|wifi|wi fi|account|software|license)|steal(?:ing)? (?:a |the |someone s |his |her |their )?"
    r"(?:password|account|credit card|identity|data))\b",
    r"\b(?:pirater|piratage|craquer le mot de passe|voler (?:le|un|son|ses) (?:mots? de passe|compte)|"
    r"voler (?:une )?identite|logiciel malveillant|rancongiciel|hameconnage)\b",
    r"\bhow (?:to|do i|can i) (?:make|build|cook|synthesi[sz]e|get away with)\b" + _w(0, 3)
    + r"(?:bomb|explosives?|meth|methamphetamine|weapons?|poison|drugs?|cocaine|napalm|gun|molotov)\b",
    r"\bcomment (?:faire|fabriquer|synthetiser|preparer)\b" + _w(0, 3)
    + r"(?:bombe|explosifs?|meth|arme|poison|drogue|cocaine|napalm)\b",
    # other products and assistants
    r"\b(?:set|mets|met|regle)\b" + _w(0, 3) + r"(?:alarm|timer|alarme|minuteur|reveil)\b",
    r"\b(?:play|joue|lance)\b" + _w(0, 3)
    + r"(?:music|musique|song|chanson|playlist|podcast|spotify|netflix|youtube|video|film|movie)\b",
    r"\b(?:send|envoie|envoyer)\b" + _w(0, 3)
    + r"(?:text|texto|sms|email|e mail|courriel|message|whatsapp)\s+(?:to|a)\b",
    r"\b(?:recommend|suggest|recommande|suggere|conseille|propose)\w*\b" + _w(0, 4)
    + r"(?:movies?|films?|series|tv shows?|books?|livres?|laptops?|phones?|ordinateurs?|podcasts?|songs?|"
    r"chansons?|video games?|jeux video|anime|recipes?|recettes?|netflix)\b",
    r"\bwhich (?:\w+\s+)?(?:laptop|phone|car|tv|iphone|android|camera|headphones|gpu)\b" + _w(0, 3)
    + r"(?:should|to|dois|devrais)\b|\bquel(?:le)?s? (?:ordinateur(?: portable)?|telephone|laptop|voiture|"
    r"appareil photo)\b.{0,15}?\b(?:dois|devrais|acheter|choisir)\b",
    r"\b(?:recipes?|recettes?)\s+(?:for|of|de|pour|du|des|d)\b",
)


@dataclass(frozen=True)
class Assessment:
    """The gate's verdict. ``mixed`` is True when a TRAVEL verdict also holds a clearly
    off-topic part; FRAME then extracts only the outing."""

    domain: DomainClass
    mixed: bool = False


def assess(text: str) -> Assessment:
    """Classify one accepted message. Deterministic, no model call, no network."""
    views = _views(text)
    if _any(_SAFETY, views):
        return Assessment(DomainClass.SAFETY)
    travel = _any(_TRAVEL_EVIDENCE, views[:1])
    if _any(_APP_HELP, views[:1]) and not travel:
        return Assessment(DomainClass.APP_HELP)
    if _any(_OFF_TOPIC, views):
        if travel:
            return Assessment(DomainClass.TRAVEL, mixed=True)
        return Assessment(DomainClass.OUT_OF_SCOPE)
    return Assessment(DomainClass.TRAVEL)


def classify(text: str) -> DomainClass:
    """TRAVEL, APP_HELP, OUT_OF_SCOPE or SAFETY. A mixed message is TRAVEL (see module doc)."""
    return assess(text).domain


def tools_for(domain: DomainClass, kind: PrincipalKind) -> tuple[str, ...]:
    if domain == DomainClass.TRAVEL:
        return ("maps_grounded_discovery", "route_estimate", "deadline_validator")
    return ()
