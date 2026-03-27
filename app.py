import os
import json
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

app = Flask(__name__)


def _init_firestore_client() -> firestore.Client:
    if not firebase_admin._apps:
        project_id = os.getenv("FIREBASE_PROJECT_ID", "brain-sync-app")
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

        if credentials_path and Path(credentials_path).exists():
            cred = credentials.Certificate(credentials_path)
            firebase_admin.initialize_app(
                credential=cred,
                options={"projectId": project_id},
            )
        else:
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


def _generate_with_gemini(topic: str, text: str) -> dict | None:
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return None

    model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    prompt = (
        "Voce e um assistente para estudo. Responda apenas com JSON valido, sem markdown. "
        "Gere os campos: question, answer, reflection, excerpt_summary, confidence. "
        "Use portugues. confidence deve ser numero entre 0 e 1. "
        f"Tema: {topic}. Texto:\n{text[:8000]}"
    )

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
        response.raise_for_status()
        data = response.json()
    except requests.RequestException:
        return None
    except ValueError:
        return None

    candidates = data.get("candidates") or []
    if not candidates:
        return None

    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or []
    raw_text = "\n".join((part.get("text") or "") for part in parts if isinstance(part, dict))
    parsed = _extract_json_object(raw_text)
    if not parsed:
        return None

    question = (parsed.get("question") or "").strip()
    answer = (parsed.get("answer") or "").strip()
    reflection = (parsed.get("reflection") or "").strip()
    excerpt_summary = (parsed.get("excerpt_summary") or "").strip()

    if not question or not answer:
        return None

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    confidence = max(0.0, min(1.0, confidence))

    return {
        "question": question,
        "answer": answer,
        "reflection": reflection or "Gerado automaticamente com Gemini.",
        "excerpt_summary": excerpt_summary or reflection,
        "ai": {
            "provider": "gemini",
            "model": model,
            "confidence": confidence,
        },
    }


def _generate_auto_content(topic: str, text: str) -> dict:
    gemini = _generate_with_gemini(topic, text)
    if gemini:
        return gemini

    question, answer, reflection = _generate_basic_card(topic, text)
    return {
        "question": question,
        "answer": answer,
        "reflection": reflection,
        "excerpt_summary": reflection,
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


@app.route("/upload-material", methods=["GET", "POST"])
def upload_material():
    if request.method == "GET":
        return render_template("upload.html")

    payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    payload = payload or {}
    uploaded_file = request.files.get("material_file") if not request.is_json else None

    chapter_raw = payload.get("chapter", "")
    content_kind = (payload.get("content_kind") or "card").strip()
    topic_raw = payload.get("topic", "")
    topic = _normalize_topic(topic_raw)
    question = (payload.get("question") or "").strip()
    answer = (payload.get("answer") or "").strip()
    reflection = (payload.get("reflection") or "").strip()
    text = (payload.get("text") or "").strip()
    source_title = (payload.get("source_title") or "").strip()
    ai_info = None
    excerpt_summary = reflection

    extracted_file_name = ""
    if uploaded_file and uploaded_file.filename:
        try:
            extracted_file_name, extracted_text = _extract_text_from_file(uploaded_file)
        except ValueError as error:
            if request.is_json:
                return jsonify({"error": str(error)}), 400
            return render_template("upload.html", error=str(error)), 400

        if not text:
            text = extracted_text
        if not source_title:
            source_title = extracted_file_name

    if content_kind == "card":
        if text and (not question or not answer):
            generated = _generate_auto_content(topic or "tema", text)
            question = question or generated["question"]
            answer = answer or generated["answer"]
            reflection = reflection or generated["reflection"]
            excerpt_summary = generated["excerpt_summary"]
            ai_info = generated["ai"]

        if not chapter_raw or not topic or not question or not answer:
            error_message = "Preencha capitulo, tema, pergunta e resposta para salvar o card."
            if request.is_json:
                return jsonify({"error": error_message}), 400
            return render_template("upload.html", error=error_message), 400
    elif content_kind == "reading_excerpt":
        if not topic or not text or not source_title:
            error_message = "Para revisao de leitura, preencha tema, titulo da fonte e trecho."
            if request.is_json:
                return jsonify({"error": error_message}), 400
            return render_template("upload.html", error=error_message), 400
    elif content_kind == "auto":
        if not topic or not text:
            error_message = "No modo automatico, informe tema e texto (ou envie arquivo TXT/PDF)."
            if request.is_json:
                return jsonify({"error": error_message}), 400
            return render_template("upload.html", error=error_message), 400

        if not source_title:
            source_title = extracted_file_name or "Material enviado"

        generated = _generate_auto_content(topic, text)
        question = generated["question"]
        answer = generated["answer"]
        reflection = reflection or generated["reflection"]
        excerpt_summary = generated["excerpt_summary"]
        ai_info = generated["ai"]
        if not chapter_raw:
            chapter_raw = "1"
    else:
        error_message = "content_kind invalido: use 'card', 'reading_excerpt' ou 'auto'."
        if request.is_json:
            return jsonify({"error": error_message}), 400
        return render_template("upload.html", error=error_message), 400

    try:
        chapter = int(chapter_raw)
    except (TypeError, ValueError):
        chapter = chapter_raw

    now = datetime.now(timezone.utc)
    docs_to_create = []

    if content_kind in ("card", "auto"):
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
            return render_template("upload.html", error=validation_error), 400

    created_ids = []
    for doc in docs_to_create:
        created_doc = db.collection("insights").add(doc)[1]
        created_ids.append(created_doc.id)

    success_message = "Material salvo com sucesso."
    if request.is_json:
        return jsonify(
            {
                "status": "ok",
                "message": success_message,
                "topic": topic,
                "kind": content_kind,
                "ids": created_ids,
                "count": len(created_ids),
            }
        ), 201
    return render_template("upload.html", success=success_message), 201


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
