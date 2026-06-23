from flask import Flask, render_template, request, redirect, url_for, send_from_directory, session
import os
from datetime import datetime

app = Flask(__name__)
app.secret_key = "supersecretkey"   # required for sessions

UPLOAD_FOLDER = "static/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

messages = []

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        username = request.form.get("username")

        # Persist username in session
        if username:
            session["username"] = username

        username = session.get("username", "Anon")

        text = request.form.get("message")
        file = request.files.get("file")

        file_path = None

        if file and file.filename:
            fname = f"{datetime.now().timestamp()}_{file.filename}"
            path = os.path.join(UPLOAD_FOLDER, fname)
            file.save(path)

            file_path = fname  # store ONLY filename
            original_name = file.filename
        else:
            file_path = None
            original_name = None

        if text or file_path:
            messages.append({
                "user": username,
                "text": text,
                "file": file_path,
                "filename": original_name
            })

        return redirect(url_for("index"))

    return render_template(
        "index.html",
        messages=messages,
        username=session.get("username", "")
    )


@app.route("/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
