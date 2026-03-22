import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import firebase_admin
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


def _serialize_doc(doc) -> dict:
    data = doc.to_dict() or {}
    metadata = data.get("metadata", {}) or {}
    next_review = metadata.get("next_review")

    if hasattr(next_review, "isoformat"):
        metadata["next_review"] = next_review.isoformat()

    data["metadata"] = metadata
    data["id"] = doc.id
    return data


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


@app.route("/")
def index():
    insight = _get_oldest_insight()
    return render_template("index.html", insight=insight)


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

    metadata = (snapshot.to_dict() or {}).get("metadata", {}) or {}

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
        "metadata.ease": new_ease,
        "metadata.interval": new_interval,
        "metadata.next_review": next_review,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }

    doc_ref.update(update_payload)

    return jsonify(
        {
            "status": "ok",
            "id": id,
            "next_review": next_review.isoformat(),
            "applied": {
                "feedback": feedback,
                "previous_ease": current_ease,
                "new_ease": new_ease,
                "previous_interval": current_interval,
                "new_interval": new_interval,
            },
        }
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    is_development = os.getenv("FLASK_ENV", "").lower() == "development"
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=is_development)
