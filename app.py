import os
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import firebase_admin
from PyPDF2 import PdfReader
import requests
from firebase_admin import credentials
from firebase_admin import firestore
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

app = Flask(__name__)


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def _get_gemini_model() -> str:
    return (os.getenv("GEMINI_MODEL") or "gemini-1.5-flash").strip()


def _parse_cors_origins() -> list[str]:
    raw_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5000")
    origins = [item.strip() for item in raw_origins.split(",") if item.strip()]
    return origins or ["http://localhost:3000", "http://localhost:5000"]


CORS(app, origins=_parse_cors_origins())


def _init_firestore_client() -> firestore.Client:
    if not firebase_admin._apps:
        project_id = os.getenv("FIREBASE_PROJECT_ID", "brain-sync-app")
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        credentials_json = (os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON") or "").strip()

        if credentials_json:
            try:
                credentials_data = json.loads(credentials_json)
            except json.JSONDecodeError as error:
                logger.error("FIREBASE_SERVICE_ACCOUNT_JSON invalido: %s", error)
                raise

            cred = credentials.Certificate(credentials_data)
            firebase_admin.initialize_app(
                credential=cred,
                options={"projectId": project_id},
            )
            return firestore.client()

        if credentials_path and Path(credentials_path).exists():
            cred = credentials.Certificate(credentials_path)
            firebase_admin.initialize_app(
                credential=cred,
                options={"projectId": project_id},
            )
        else:
            # Remove path quebrado para nao forcar ADC em arquivo inexistente.
            if credentials_path and not Path(credentials_path).exists():
                os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            firebase_admin.initialize_app(options={"projectId": project_id})

    return firestore.client()


db = _init_firestore_client()


def _normalize_topic(value: str) -> str:
    topic = (value or "").strip().lower()
    topic = re.sub(r"\s+", "-", topic)
    topic = re.sub(r"[^a-z0-9\-]", "", topic)
    topic = re.sub(r"-+", "-", topic).strip("-")
    return topic


def _validate_card_payload(doc: dict) -> str | None:
    kind = doc.get("kind")
    if kind != "card":
        return "kind deve ser 'card'"

    topic = doc.get("topic")
    if not isinstance(topic, str) or not topic:
        return "topic obrigatorio para kind=card"

    active_recall = doc.get("active_recall") or {}
    question = (active_recall.get("pergunta") or "").strip()
    answer = (active_recall.get("resposta") or "").strip()
    if not question or not answer:
        return "active_recall.pergunta e active_recall.resposta sao obrigatorios"

    metadata = doc.get("metadata") or {}
    if "next_review" not in metadata:
        return "metadata.next_review obrigatorio para kind=card"

    return None


def _validate_reading_excerpt_payload(doc: dict) -> str | None:
    kind = doc.get("kind")
    if kind != "reading_excerpt":
        return "kind deve ser 'reading_excerpt'"

    topic = doc.get("topic")
    if not isinstance(topic, str) or not topic:
        return "topic obrigatorio para kind=reading_excerpt"

    content = doc.get("content") or {}
    if not (content.get("text") or "").strip():
        return "content.text obrigatorio para kind=reading_excerpt"

    return None


def _validate_insight_payload(doc: dict) -> str | None:
    kind = doc.get("kind")
    if kind == "card":
        return _validate_card_payload(doc)
    if kind == "reading_excerpt":
        return _validate_reading_excerpt_payload(doc)
    return "kind invalido: use 'card' ou 'reading_excerpt'"


def _extract_text_from_file(uploaded_file) -> tuple[str, str]:
    filename = (uploaded_file.filename or "").strip()
    if not filename:
        raise ValueError("arquivo sem nome")

    lower_name = filename.lower()
    if lower_name.endswith(".txt"):
        file_bytes = uploaded_file.read()
        text = file_bytes.decode("utf-8", errors="replace")
        return filename, text.strip()

    if lower_name.endswith(".pdf"):
        reader = PdfReader(uploaded_file)
        pages_text: list[str] = []
        for page in reader.pages:
            pages_text.append(page.extract_text() or "")
        return filename, "\n".join(pages_text).strip()

    raise ValueError("formato nao suportado: use .txt ou .pdf")


def _generate_basic_card(topic: str, text: str) -> tuple[str, str, str]:
    clean_text = re.sub(r"\s+", " ", text).strip()
    snippet = clean_text[:480]
    question = f"Quais sao os principais pontos sobre {topic.replace('-', ' ')}?"
    answer = snippet if snippet else "Sem conteudo suficiente para resumir."
    reflection = "Card gerado automaticamente a partir do material enviado."
    return question, answer, reflection


def _truncate_chars(value: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", (value or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _normalize_study_cards(raw_cards) -> list[dict]:
    cards: list[dict] = []
    if not isinstance(raw_cards, list):
        return cards

    for item in raw_cards:
        if not isinstance(item, dict):
            continue
        question = _truncate_chars((item.get("question") or "").strip(), 110)
        answer = _truncate_chars((item.get("answer") or "").strip(), 180)
        if not question or not answer:
            continue
        cards.append({"question": question, "answer": answer})

    return cards[:6]


def _normalize_review_items(raw_items, full_text: str) -> list[dict]:
    items: list[dict] = []
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            summary = _truncate_chars((item.get("summary") or "").strip(), 220)
            excerpt = _truncate_chars((item.get("excerpt") or "").strip(), 1200)
            if not summary and not excerpt:
                continue
            items.append(
                {
                    "summary": summary or excerpt,
                    "excerpt": excerpt or summary,
                }
            )

    if items:
        return items[:4]

    fallback_excerpt = _truncate_chars(full_text, 1200)
    fallback_summary = _truncate_chars(full_text, 220)
    return [{"summary": fallback_summary, "excerpt": fallback_excerpt}] if fallback_excerpt else []


def _build_fallback_study_cards(topic: str, text: str) -> list[dict]:
    clean_text = re.sub(r"\s+", " ", (text or "")).strip()
    sentences = [chunk.strip() for chunk in re.split(r"(?<=[.!?])\s+", clean_text) if chunk.strip()]

    templates = [
        f"Qual ideia central sobre {topic.replace('-', ' ')}?",
        "Qual pratica ajuda na retencao desse conteudo?",
        "Que resultado esperado esse tema traz no estudo?",
    ]

    cards: list[dict] = []
    for idx, sentence in enumerate(sentences[:3]):
        question = templates[idx] if idx < len(templates) else "Qual ponto chave deste conteudo?"
        answer = _truncate_chars(sentence, 180)
        if answer:
            cards.append({"question": _truncate_chars(question, 110), "answer": answer})

    if cards:
        return cards

    question, answer, _ = _generate_basic_card(topic, clean_text)
    return [{"question": _truncate_chars(question, 110), "answer": _truncate_chars(answer, 180)}]


def _build_fallback_review_items(text: str) -> list[dict]:
    clean_text = re.sub(r"\s+", " ", (text or "")).strip()
    if not clean_text:
        return []

    max_chunk = 700
    chunks = [clean_text[i : i + max_chunk] for i in range(0, min(len(clean_text), max_chunk * 2), max_chunk)]
    items = []
    for chunk in chunks:
        excerpt = _truncate_chars(chunk, 1200)
        summary = _truncate_chars(chunk, 220)
        if excerpt:
            items.append({"summary": summary, "excerpt": excerpt})

    return items[:2]


def _extract_json_object(text: str) -> dict | None:
    if not text:
        return None

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    raw_json = text[start : end + 1]
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    return parsed


def _build_study_prompt(topic: str, text: str) -> str:
    return (
        "System Instruction: "
        "Atue como um Especialista em Ciencias do Aprendizado e Design Instrucional. "
        "Sua tarefa e extrair flashcards de alta qualidade do texto fornecido. "
        "Regras de Extracao: "
        "Identificacao de Pares: procure por definicoes, processos, causas e efeitos. "
        "Logica de Notas: se houver nota manuscrita/comentario do usuario, use como pergunta; "
        "se houver texto entre aspas/destaque, use como resposta. "
        "Principio da Atomicidade: cada card deve conter apenas uma pergunta simples e direta; "
        "evite listas longas na resposta. "
        "Contexto: adicione sempre o campo tema baseado no assunto central do paragrafo. "
        "Responda apenas JSON valido, sem markdown, com o formato: "
        "{topic, understanding, reflection, confidence, study_cards:[{question,answer}]}. "
        "As perguntas e respostas devem ser curtas e objetivas. "
        f"Tema sugerido: {topic}. Texto: {text[:8000]}"
    )


def _build_review_prompt(topic: str, text: str) -> str:
    return (
        "System Instruction: "
        "Atue como um Mentor de Produtividade e Pesquisador Academico. "
        "Sua tarefa e extrair insights de coaching e reflexoes de literatura do texto fornecido. "
        "Regras de Extracao: "
        "Filtro de Relevancia: ignore fatos puramente tecnicos e foque em principios, heuristicas, "
        "conselhos filosoficos, observacoes sobre comportamento e frases de impacto. "
        "Sintese de Valor: transforme paragrafos densos em uma unica frase poderosa que resuma o insight (content). "
        "Provocacao: no campo reflection, crie uma pergunta que force o usuario a aplicar aquele pensamento na realidade atual (estilo GTD). "
        "Categorizacao: identifique o tema. "
        "Responda apenas JSON valido, sem markdown, com o formato: "
        "{topic, understanding, confidence, review_items:[{summary,excerpt,reflection}]}. "
        f"Tema sugerido: {topic}. Texto: {text[:8000]}"
    )


def _call_gemini_json(prompt: str, api_key: str, model: str, endpoint: str) -> dict | None:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json",
        },
    }

    try:
        response = requests.post(
            endpoint,
            params={"key": api_key},
            json=payload,
            timeout=25,
        )
        if response.status_code == 400:
            fallback_payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.3,
                },
            }
            response = requests.post(
                endpoint,
                params={"key": api_key},
                json=fallback_payload,
                timeout=25,
            )

        response.raise_for_status()
        data = response.json()
    except requests.RequestException as error:
        logger.warning("Falha na chamada Gemini: %s", type(error).__name__)
        return None
    except ValueError:
        logger.warning("Resposta invalida do Gemini")
        return None

    candidates = data.get("candidates") or []
    if not candidates:
        return None

    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or []
    raw_text = "\n".join((part.get("text") or "") for part in parts if isinstance(part, dict))
    return _extract_json_object(raw_text)


def _generate_with_gemini(topic: str, text: str, target_kind: str = "auto") -> dict | None:
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return None

    model = _get_gemini_model()
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    parsed_study = None
    parsed_review = None
    if target_kind in ("auto", "card", "study"):
        parsed_study = _call_gemini_json(_build_study_prompt(topic, text), api_key, model, endpoint)
    if target_kind in ("auto", "reading_excerpt", "review"):
        parsed_review = _call_gemini_json(_build_review_prompt(topic, text), api_key, model, endpoint)

    if not parsed_study and not parsed_review:
        return None

    topic_suggestion = _normalize_topic(
        ((parsed_study or {}).get("topic") or (parsed_review or {}).get("topic") or "").strip()
    )
    understanding = (
        ((parsed_study or {}).get("understanding") or "").strip()
        or ((parsed_review or {}).get("understanding") or "").strip()
    )
    reflection = (
        ((parsed_study or {}).get("reflection") or "").strip()
        or ((parsed_review or {}).get("reflection") or "").strip()
    )

    study_cards = _normalize_study_cards((parsed_study or {}).get("study_cards"))
    review_items = _normalize_review_items((parsed_review or {}).get("review_items"), text)

    if parsed_review and not review_items:
        review_items = _normalize_review_items(
            [
                {
                    "summary": (parsed_review.get("content") or "").strip(),
                    "excerpt": _truncate_chars(text, 1200),
                    "reflection": (parsed_review.get("reflection") or "").strip(),
                }
            ],
            text,
        )

    try:
        confidence_study = float((parsed_study or {}).get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence_study = 0.0
    try:
        confidence_review = float((parsed_review or {}).get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence_review = 0.0

    confidence_values = [value for value in [confidence_study, confidence_review] if value > 0]
    confidence = (sum(confidence_values) / len(confidence_values)) if confidence_values else 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "study_cards": study_cards,
        "review_items": review_items,
        "reflection": reflection or "Gerado automaticamente com Gemini.",
        "topic_suggestion": topic_suggestion,
        "understanding": understanding or reflection,
        "ai": {
            "provider": "gemini",
            "model": model,
            "confidence": confidence,
        },
    }


def _generate_auto_content(topic: str, text: str, prefer_gemini: bool = True, target_kind: str = "auto") -> dict:
    gemini = _generate_with_gemini(topic, text, target_kind=target_kind) if prefer_gemini else None
    if gemini:
        return gemini

    _, answer, reflection = _generate_basic_card(topic, text)
    fallback_cards = _build_fallback_study_cards(topic, text)
    fallback_reviews = _build_fallback_review_items(text)
    return {
        "study_cards": fallback_cards if target_kind in ("auto", "card", "study") else [],
        "review_items": fallback_reviews if target_kind in ("auto", "reading_excerpt", "review") else [],
        "reflection": reflection,
        "topic_suggestion": _normalize_topic(topic),
        "understanding": _truncate_chars(answer, 220),
        "ai": {
            "provider": "basic-fallback",
            "model": "heuristic",
            "confidence": 0.0,
        },
    }


def _serialize_doc(doc) -> dict:
    data = doc.to_dict() or {}
    metadata = data.get("metadata", {}) or {}
    next_review = metadata.get("next_review")

    if hasattr(next_review, "isoformat"):
        metadata["next_review"] = next_review.isoformat()

    data["metadata"] = metadata
    data["id"] = doc.id
    return data


def _touch_seen(doc_ref) -> None:
    doc_ref.update(
        {
            "last_seen_at": datetime.now(timezone.utc),
            "seen_count": firestore.Increment(1),
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )


def _get_oldest_insight() -> dict | None:
    doc_ref = (
        db.collection("insights")
        .order_by("metadata.next_review")
        .limit(1)
        .stream()
    )
    oldest_doc = next(doc_ref, None)

    if oldest_doc is None:
        return None

    return _serialize_doc(oldest_doc)


def _get_next_card_for_topic(topic: str) -> dict | None:
    now = datetime.now(timezone.utc)

    # Avoid hard dependency on a composite index during early development.
    ordered_docs = (
        db.collection("insights")
        .order_by("metadata.next_review")
        .limit(200)
        .stream()
    )

    for doc in ordered_docs:
        data = doc.to_dict() or {}
        if data.get("kind") != "card":
            continue
        if data.get("topic") != topic:
            continue

        next_review = ((data.get("metadata") or {}).get("next_review"))
        if isinstance(next_review, datetime) and next_review <= now:
            return _serialize_doc(doc)

    return None


def _list_card_candidates(topic: str | None = None) -> list[dict]:
    docs = db.collection("insights").where("kind", "==", "card").limit(500).stream()

    candidates: list[dict] = []
    for doc in docs:
        data = doc.to_dict() or {}
        doc_topic = data.get("topic")
        if topic and doc_topic != topic:
            continue

        candidates.append(_serialize_doc(doc))

    return candidates


def _card_study_sort_key(item: dict):
    seen_count = item.get("seen_count", 0) or 0
    last_seen_at = item.get("last_seen_at")
    created_at = item.get("created_at")
    last_seen_is_none = 0 if last_seen_at is None else 1
    fallback_time = datetime.max
    return (
        int(seen_count),
        last_seen_is_none,
        last_seen_at if isinstance(last_seen_at, datetime) else fallback_time,
        created_at if isinstance(created_at, datetime) else fallback_time,
    )


def _get_next_card_for_study(mode: str, topic: str | None = None) -> dict | None:
    candidates = _list_card_candidates(topic=topic)
    if not candidates:
        return None

    ordered = sorted(candidates, key=_card_study_sort_key)
    if mode == "shuffle":
        top_slice = ordered[: min(25, len(ordered))]
        return random.choice(top_slice)

    return ordered[0]


def _list_reading_candidates(topic: str | None = None) -> list[dict]:
    docs = db.collection("insights").where("kind", "==", "reading_excerpt").limit(500).stream()

    candidates: list[dict] = []
    for doc in docs:
        data = doc.to_dict() or {}
        doc_topic = data.get("topic")
        if topic and doc_topic != topic:
            continue

        serialized = _serialize_doc(doc)
        candidates.append(serialized)

    return candidates


def _reading_sort_key(item: dict):
    seen_count = item.get("seen_count", 0) or 0
    last_seen_at = item.get("last_seen_at")
    created_at = item.get("created_at")
    last_seen_is_none = 0 if last_seen_at is None else 1
    fallback_time = datetime.max
    return (
        int(seen_count),
        last_seen_is_none,
        last_seen_at if isinstance(last_seen_at, datetime) else fallback_time,
        created_at if isinstance(created_at, datetime) else fallback_time,
    )


def _get_next_reading_excerpt(mode: str, topic: str | None = None) -> dict | None:
    candidates = _list_reading_candidates(topic=topic)
    if not candidates:
        return None

    ordered = sorted(candidates, key=_reading_sort_key)

    if mode == "shuffle":
        top_slice = ordered[: min(25, len(ordered))]
        return random.choice(top_slice)

    return ordered[0]


def _list_topics(kind: str | None = None) -> list[str]:
    docs = db.collection("insights").limit(1000).stream()
    topics = set()

    for doc in docs:
        data = doc.to_dict() or {}
        if kind and data.get("kind") != kind:
            continue

        topic = data.get("topic")
        if isinstance(topic, str) and topic.strip():
            topics.add(topic.strip())

    return sorted(topics)


def _list_cards_for_management(topic: str | None = None, limit: int = 300) -> list[dict]:
    docs = db.collection("insights").where("kind", "==", "card").limit(limit).stream()
    cards: list[dict] = []

    for doc in docs:
        data = doc.to_dict() or {}
        if topic and data.get("topic") != topic:
            continue
        cards.append(_serialize_doc(doc))

    cards.sort(
        key=lambda item: item.get("created_at") if isinstance(item.get("created_at"), datetime) else datetime.min,
        reverse=True,
    )
    return cards


def _topic_card_summary(cards: list[dict]) -> list[dict]:
    summary: dict[str, int] = {}
    for card in cards:
        topic = (card.get("topic") or "").strip()
        if not topic:
            continue
        summary[topic] = summary.get(topic, 0) + 1

    return [
        {"topic": topic, "cards_count": count}
        for topic, count in sorted(summary.items(), key=lambda item: item[0])
    ]


def _parse_generated_items(raw_value: str | None) -> list[dict]:
    if not raw_value:
        return []

    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError):
        return []

    if not isinstance(parsed, list):
        return []

    return [item for item in parsed if isinstance(item, dict)]


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/revisao-leituras")
def revisao_leituras():
    current_topic = _normalize_topic(request.args.get("topic", ""))
    return render_template("reading_review.html", current_topic=current_topic)


@app.route("/estudo")
def estudo():
    current_topic = _normalize_topic(request.args.get("topic", ""))
    return render_template("study.html", current_topic=current_topic)


@app.route("/api/materials/preview", methods=["POST"])
def preview_materials_with_ai():
    payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    payload = payload or {}
    uploaded_file = request.files.get("material_file") if not request.is_json else None

    target_kind = (payload.get("target_kind") or "card").strip().lower()
    if target_kind not in ("card", "reading_excerpt"):
        return jsonify({"status": "error", "error": "target_kind invalido: use card ou reading_excerpt"}), 400

    topic = _normalize_topic(payload.get("topic", ""))
    text = (payload.get("text") or "").strip()
    source_title = (payload.get("source_title") or "").strip()
    use_gemini_raw = (payload.get("use_gemini", "1") or "").strip().lower()
    use_gemini = use_gemini_raw in ("1", "true", "on", "yes", "gemini")

    extracted_file_name = ""
    if uploaded_file and uploaded_file.filename:
        try:
            extracted_file_name, extracted_text = _extract_text_from_file(uploaded_file)
        except ValueError as error:
            return jsonify({"status": "error", "error": str(error)}), 400
        if not text:
            text = extracted_text
        if not source_title:
            source_title = extracted_file_name

    if not text:
        return jsonify({"status": "error", "error": "Informe texto ou arquivo para processar com IA."}), 400

    if not source_title:
        source_title = extracted_file_name or "Material enviado"

    generated = _generate_auto_content(topic or "geral", text, prefer_gemini=use_gemini, target_kind=target_kind)
    if not topic:
        topic = _normalize_topic(generated.get("topic_suggestion") or source_title or "geral") or "geral"

    if target_kind == "card":
        preview_items = _normalize_study_cards(generated.get("study_cards"))
    else:
        preview_items = _normalize_review_items(generated.get("review_items"), text)

    if not preview_items:
        return jsonify({"status": "error", "error": "IA nao retornou itens suficientes para pre-visualizacao."}), 422

    return jsonify(
        {
            "status": "ok",
            "target_kind": target_kind,
            "topic": topic,
            "source_title": source_title,
            "text": text,
            "understanding": generated.get("understanding") or "",
            "reflection": generated.get("reflection") or "",
            "ai": generated.get("ai") or {},
            "items": preview_items,
        }
    ), 200


@app.route("/upload-material", methods=["GET", "POST"])
def upload_material():
    if request.method == "GET":
        cards = _list_cards_for_management(limit=300)
        return render_template(
            "upload.html",
            cards=cards,
            topics_summary=_topic_card_summary(cards),
            generated_counts=None,
        )

    payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    payload = payload or {}
    uploaded_file = request.files.get("material_file") if not request.is_json else None

    chapter_raw = payload.get("chapter", "")
    content_kind = (payload.get("content_kind") or "auto").strip()
    topic_raw = payload.get("topic", "")
    topic = _normalize_topic(topic_raw)
    question = (payload.get("question") or "").strip()
    answer = (payload.get("answer") or "").strip()
    reflection = (payload.get("reflection") or "").strip()
    text = (payload.get("text") or "").strip()
    source_title = (payload.get("source_title") or "").strip()
    use_gemini_raw = (payload.get("use_gemini", "1") or "").strip().lower()
    use_gemini = use_gemini_raw in ("1", "true", "on", "yes", "gemini")
    generation_mode = (payload.get("generation_mode") or "manual").strip().lower()
    generated_items = _parse_generated_items(payload.get("generated_items_json"))
    ai_info = None
    excerpt_summary = reflection
    gemini_understanding = ""
    auto_study_cards: list[dict] = []
    auto_review_items: list[dict] = []

    extracted_file_name = ""
    if uploaded_file and uploaded_file.filename:
        try:
            extracted_file_name, extracted_text = _extract_text_from_file(uploaded_file)
        except ValueError as error:
            if request.is_json:
                return jsonify({"error": str(error)}), 400
            cards = _list_cards_for_management(limit=300)
            return render_template(
                "upload.html",
                error=str(error),
                cards=cards,
                topics_summary=_topic_card_summary(cards),
            ), 400

        if not text:
            text = extracted_text
        if not source_title:
            source_title = extracted_file_name

    if content_kind == "card":
        if generation_mode == "gemini" and generated_items:
            auto_study_cards = _normalize_study_cards(generated_items)
            gemini_understanding = (payload.get("gemini_understanding") or "").strip()
            reflection = reflection or (payload.get("reflection") or "").strip() or "Gerado com Gemini."
            if not source_title:
                source_title = extracted_file_name or "Material processado com IA"
            if not topic:
                topic = "geral"
            if not chapter_raw:
                chapter_raw = "1"
            ai_info = {
                "provider": "gemini",
                "model": _get_gemini_model(),
                "confidence": 0.0,
            }
        elif text and (not question or not answer):
            generated = _generate_auto_content(topic or "tema", text, prefer_gemini=use_gemini, target_kind="card")
            generated_study = generated.get("study_cards") or []
            if generated_study:
                question = question or generated_study[0].get("question", "")
                answer = answer or generated_study[0].get("answer", "")
            reflection = reflection or generated["reflection"]
            ai_info = generated["ai"]

        if generation_mode == "gemini" and generated_items:
            if not chapter_raw or not topic or not auto_study_cards:
                error_message = "Pre-visualizacao invalida. Processe com IA novamente para gerar cards de estudo."
                if request.is_json:
                    return jsonify({"error": error_message}), 400
                cards = _list_cards_for_management(limit=300)
                return render_template(
                    "upload.html",
                    error=error_message,
                    cards=cards,
                    topics_summary=_topic_card_summary(cards),
                ), 400
        elif not chapter_raw or not topic or not question or not answer:
            error_message = "Preencha capitulo, tema, pergunta e resposta para salvar o card."
            if request.is_json:
                return jsonify({"error": error_message}), 400
            cards = _list_cards_for_management(limit=300)
            return render_template(
                "upload.html",
                error=error_message,
                cards=cards,
                topics_summary=_topic_card_summary(cards),
            ), 400
    elif content_kind == "reading_excerpt":
        if generation_mode == "gemini" and generated_items:
            auto_review_items = _normalize_review_items(generated_items, text)
            gemini_understanding = (payload.get("gemini_understanding") or "").strip()
            excerpt_summary = auto_review_items[0]["summary"] if auto_review_items else excerpt_summary
            if not source_title:
                source_title = extracted_file_name or "Material processado com IA"
            if not topic:
                topic = "geral"
            ai_info = {
                "provider": "gemini",
                "model": _get_gemini_model(),
                "confidence": 0.0,
            }
            if not auto_review_items:
                error_message = "Pre-visualizacao invalida. Processe com IA novamente para gerar revisoes."
                if request.is_json:
                    return jsonify({"error": error_message}), 400
                cards = _list_cards_for_management(limit=300)
                return render_template(
                    "upload.html",
                    error=error_message,
                    cards=cards,
                    topics_summary=_topic_card_summary(cards),
                ), 400
        elif not topic or not text or not source_title:
            error_message = "Para revisao de leitura, preencha tema, titulo da fonte e trecho."
            if request.is_json:
                return jsonify({"error": error_message}), 400
            cards = _list_cards_for_management(limit=300)
            return render_template(
                "upload.html",
                error=error_message,
                cards=cards,
                topics_summary=_topic_card_summary(cards),
            ), 400
    elif content_kind == "auto":
        if not text:
            error_message = "Envie um arquivo TXT/PDF ou cole um texto para processar com Gemini."
            if request.is_json:
                return jsonify({"error": error_message}), 400
            cards = _list_cards_for_management(limit=300)
            return render_template(
                "upload.html",
                error=error_message,
                cards=cards,
                topics_summary=_topic_card_summary(cards),
            ), 400

        if not source_title:
            source_title = extracted_file_name or "Material enviado"

        generated = _generate_auto_content(topic or "geral", text, prefer_gemini=use_gemini, target_kind="auto")
        auto_study_cards = generated.get("study_cards") or []
        auto_review_items = generated.get("review_items") or []
        if not auto_study_cards:
            error_message = "Nao foi possivel gerar cards de estudo a partir do material enviado."
            if request.is_json:
                return jsonify({"error": error_message}), 400
            cards = _list_cards_for_management(limit=300)
            return render_template(
                "upload.html",
                error=error_message,
                cards=cards,
                topics_summary=_topic_card_summary(cards),
            ), 400

        reflection = reflection or generated["reflection"]
        excerpt_summary = auto_review_items[0]["summary"] if auto_review_items else reflection
        gemini_understanding = generated.get("understanding") or excerpt_summary
        if not topic:
            topic = _normalize_topic(generated.get("topic_suggestion") or source_title or "geral")
        if not topic:
            topic = "geral"
        ai_info = generated["ai"]
        if not chapter_raw:
            chapter_raw = "1"
    else:
        error_message = "content_kind invalido: use 'card', 'reading_excerpt' ou 'auto'."
        if request.is_json:
            return jsonify({"error": error_message}), 400
        cards = _list_cards_for_management(limit=300)
        return render_template(
            "upload.html",
            error=error_message,
            cards=cards,
            topics_summary=_topic_card_summary(cards),
        ), 400

    try:
        chapter = int(chapter_raw)
    except (TypeError, ValueError):
        chapter = chapter_raw

    now = datetime.now(timezone.utc)
    docs_to_create = []

    if content_kind in ("card", "auto"):
        generate_multiple_study = content_kind == "auto" or (content_kind == "card" and generation_mode == "gemini" and auto_study_cards)
        if generate_multiple_study:
            for generated_card in auto_study_cards:
                card_payload = {
                    "kind": "card",
                    "topic": topic,
                    "status": "ready",
                    "seen_count": 0,
                    "last_seen_at": None,
                    "chapter": chapter,
                    "text": text,
                    "reflection": reflection,
                    "active_recall": {
                        "pergunta": generated_card["question"],
                        "resposta": generated_card["answer"],
                    },
                    "difficulty": {
                        "last_feedback": None,
                    },
                    "metadata": {
                        "ease": 2.5,
                        "interval": 0,
                        "next_review": now,
                        "last_review_at": None,
                    },
                    "source": {
                        "title": source_title,
                        "file_name": extracted_file_name,
                    },
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                }
                if ai_info:
                    card_payload["ai"] = ai_info
                docs_to_create.append(card_payload)
        else:
            card_payload = {
                "kind": "card",
                "topic": topic,
                "status": "ready",
                "seen_count": 0,
                "last_seen_at": None,
                "chapter": chapter,
                "text": text,
                "reflection": reflection,
                "active_recall": {
                    "pergunta": question,
                    "resposta": answer,
                },
                "difficulty": {
                    "last_feedback": None,
                },
                "metadata": {
                    "ease": 2.5,
                    "interval": 0,
                    "next_review": now,
                    "last_review_at": None,
                },
                "source": {
                    "title": source_title,
                    "file_name": extracted_file_name,
                },
                "created_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
            if ai_info:
                card_payload["ai"] = ai_info
            docs_to_create.append(card_payload)

    if content_kind in ("reading_excerpt", "auto"):
        generate_multiple_review = (content_kind == "auto" and auto_review_items) or (
            content_kind == "reading_excerpt" and generation_mode == "gemini" and auto_review_items
        )
        if generate_multiple_review:
            for review_item in auto_review_items:
                excerpt_payload = {
                    "kind": "reading_excerpt",
                    "topic": topic,
                    "status": "ready",
                    "seen_count": 0,
                    "last_seen_at": None,
                    "content": {
                        "text": review_item["excerpt"],
                        "summary": review_item["summary"],
                    },
                    "source": {
                        "title": source_title,
                        "file_name": extracted_file_name,
                    },
                    "reading": {
                        "mode_eligible": "both",
                        "order_key": now,
                    },
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                }
                if ai_info:
                    excerpt_payload["ai"] = ai_info
                docs_to_create.append(excerpt_payload)
        else:
            excerpt_payload = {
                "kind": "reading_excerpt",
                "topic": topic,
                "status": "ready",
                "seen_count": 0,
                "last_seen_at": None,
                "content": {
                    "text": text,
                    "summary": excerpt_summary,
                },
                "source": {
                    "title": source_title,
                    "file_name": extracted_file_name,
                },
                "reading": {
                    "mode_eligible": "both",
                    "order_key": now,
                },
                "created_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
            if ai_info:
                excerpt_payload["ai"] = ai_info
            docs_to_create.append(excerpt_payload)

    for doc in docs_to_create:
        validation_error = _validate_insight_payload(doc)
        if validation_error:
            if request.is_json:
                return jsonify({"status": "error", "error": validation_error}), 400
            cards = _list_cards_for_management(limit=300)
            return render_template(
                "upload.html",
                error=validation_error,
                cards=cards,
                topics_summary=_topic_card_summary(cards),
            ), 400

    created_ids = []
    for doc in docs_to_create:
        created_doc = db.collection("insights").add(doc)[1]
        created_ids.append(created_doc.id)

    study_generated_count = sum(1 for doc in docs_to_create if doc.get("kind") == "card")
    review_generated_count = sum(1 for doc in docs_to_create if doc.get("kind") == "reading_excerpt")

    logger.info(
        "Material salvo topic=%s kind=%s ids=%s",
        topic,
        content_kind,
        ",".join(created_ids),
    )

    success_message = (
        "Material salvo com sucesso. "
        f"{len(created_ids)} itens gerados "
        f"({study_generated_count} estudo, {review_generated_count} revisao)."
    )
    if request.is_json:
        return jsonify(
            {
                "status": "ok",
                "message": success_message,
                "topic": topic,
                "kind": content_kind,
                "ids": created_ids,
                "count": len(created_ids),
                "generated_counts": {
                    "study": study_generated_count,
                    "review": review_generated_count,
                },
                "gemini_understanding": gemini_understanding,
            }
        ), 201
    cards = _list_cards_for_management(limit=300)
    return render_template(
        "upload.html",
        success=success_message,
        cards=cards,
        topics_summary=_topic_card_summary(cards),
        generated_counts={"study": study_generated_count, "review": review_generated_count},
        gemini_understanding=gemini_understanding,
    ), 201


@app.route("/api/cards/manage", methods=["GET"])
def list_cards_for_management():
    topic = _normalize_topic(request.args.get("topic", ""))
    cards = _list_cards_for_management(topic=topic if topic else None, limit=400)
    return jsonify(
        {
            "status": "ok",
            "topic": topic or None,
            "cards": cards,
            "topics": _topic_card_summary(cards if topic else _list_cards_for_management(limit=600)),
        }
    ), 200


@app.route("/api/cards/<id>", methods=["DELETE"])
def delete_card(id: str):
    doc_ref = db.collection("insights").document(id)
    snapshot = doc_ref.get()

    if not snapshot.exists:
        return jsonify({"status": "error", "error": "Card nao encontrado", "id": id}), 404

    doc = snapshot.to_dict() or {}
    if doc.get("kind") != "card":
        return jsonify({"status": "error", "error": "Somente documentos kind=card podem ser excluidos"}), 400

    doc_ref.delete()
    return jsonify({"status": "ok", "message": "Card excluido", "id": id}), 200


@app.route("/api/cards/review", methods=["GET"])
def next_card_for_review():
    topic = _normalize_topic(request.args.get("topic", ""))
    if not topic:
        return jsonify({"status": "error", "error": "query param topic e obrigatorio"}), 400

    card = _get_next_card_for_topic(topic)
    return jsonify({"status": "ok", "topic": topic, "card": card}), 200


@app.route("/api/cards/study", methods=["GET"])
def next_card_for_study():
    mode = (request.args.get("mode", "topic") or "topic").strip().lower()
    if mode not in ("topic", "shuffle"):
        return jsonify({"status": "error", "error": "mode invalido: use topic ou shuffle"}), 400

    topic = _normalize_topic(request.args.get("topic", ""))
    if mode == "topic" and not topic:
        return jsonify({"status": "error", "error": "query param topic e obrigatorio no modo topic"}), 400

    card = _get_next_card_for_study(mode=mode, topic=topic if topic else None)
    return jsonify({"status": "ok", "mode": mode, "topic": topic, "card": card}), 200


@app.route("/api/topics", methods=["GET"])
def list_topics():
    kind = (request.args.get("kind", "") or "").strip().lower()
    if kind and kind not in ("card", "reading_excerpt"):
        return jsonify({"status": "error", "error": "kind invalido: use card ou reading_excerpt"}), 400

    topics = _list_topics(kind=kind if kind else None)
    return jsonify({"status": "ok", "kind": kind or None, "topics": topics}), 200


@app.route("/api/readings/next", methods=["GET"])
def next_reading_excerpt():
    mode = (request.args.get("mode", "topic") or "topic").strip().lower()
    if mode not in ("topic", "shuffle"):
        return jsonify({"status": "error", "error": "mode invalido: use topic ou shuffle"}), 400

    topic = _normalize_topic(request.args.get("topic", ""))
    if mode == "topic" and not topic:
        return jsonify({"status": "error", "error": "query param topic e obrigatorio no modo topic"}), 400

    excerpt = _get_next_reading_excerpt(mode=mode, topic=topic if topic else None)
    return jsonify({"status": "ok", "mode": mode, "topic": topic, "excerpt": excerpt}), 200


@app.route("/api/insights/<id>/seen", methods=["POST"])
def mark_insight_seen(id: str):
    doc_ref = db.collection("insights").document(id)
    snapshot = doc_ref.get()

    if not snapshot.exists:
        return jsonify({"status": "error", "error": "Insight nao encontrado", "id": id}), 404

    _touch_seen(doc_ref)
    return jsonify({"status": "ok", "id": id, "message": "Visualizacao registrada"}), 200


@app.route("/api/next-insight", methods=["GET"])
def next_insight():
    insight = _get_oldest_insight()
    return jsonify({"status": "ok", "insight": insight}), 200


@app.route("/review/<id>", methods=["POST"])
def review(id: str):
    payload = request.get_json(silent=True) or request.form.to_dict() or {}

    doc_ref = db.collection("insights").document(id)
    snapshot = doc_ref.get()

    if not snapshot.exists:
        return jsonify({"error": "Insight não encontrado", "id": id}), 404

    feedback_value = payload.get("feedback", payload.get("ease"))

    try:
        feedback = int(feedback_value)
    except (TypeError, ValueError):
        try:
            feedback = int(payload.get("ease"))
        except (TypeError, ValueError):
            return jsonify({"error": "feedback deve ser 1, 2 ou 3"}), 400

    if feedback not in (1, 2, 3):
        return jsonify({"error": "feedback deve ser 1, 2 ou 3"}), 400

    current_doc = snapshot.to_dict() or {}
    if current_doc.get("kind") not in (None, "card"):
        return jsonify({"status": "error", "error": "somente kind=card aceita feedback", "id": id}), 400

    metadata = current_doc.get("metadata", {}) or {}

    try:
        current_ease = float(metadata.get("ease", 2.5))
    except (TypeError, ValueError):
        current_ease = 2.5

    try:
        current_interval = int(metadata.get("interval", 0))
    except (TypeError, ValueError):
        current_interval = 0

    current_interval = max(current_interval, 0)

    if feedback == 1:
        new_ease = current_ease - 0.2
    elif feedback == 2:
        new_ease = current_ease
    else:
        new_ease = current_ease + 0.15

    new_ease = max(1.3, new_ease)

    if feedback == 1:
        new_interval = 1
    elif current_interval == 0:
        new_interval = 3
    else:
        new_interval = max(1, int(round(current_interval * new_ease)))

    next_review = datetime.now(timezone.utc) + timedelta(days=new_interval)

    update_payload = {
        "difficulty.last_feedback": feedback,
        "metadata.ease": new_ease,
        "metadata.interval": new_interval,
        "metadata.next_review": next_review,
        "metadata.last_review_at": datetime.now(timezone.utc),
        "last_seen_at": datetime.now(timezone.utc),
        "seen_count": firestore.Increment(1),
        "updated_at": firestore.SERVER_TIMESTAMP,
    }

    doc_ref.update(update_payload)

    logger.info(
        "Feedback aplicado id=%s feedback=%s interval=%s ease=%.2f",
        id,
        feedback,
        new_interval,
        new_ease,
    )

    response_payload = {
        "status": "ok",
        "message": "Feedback aplicado com sucesso",
        "id": id,
        "next_review": next_review.isoformat(),
        "applied": {
            "feedback": feedback,
            "previous_ease": current_ease,
            "new_ease": new_ease,
            "previous_interval": current_interval,
            "new_interval": new_interval,
        },
        "data": {
            "card_id": id,
            "feedback": feedback,
            "next_review": next_review.isoformat(),
            "schedule": {
                "ease": new_ease,
                "interval": new_interval,
            },
        },
    }
    return jsonify(response_payload), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    is_development = os.getenv("FLASK_ENV", "").lower() == "development"
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=is_development)
