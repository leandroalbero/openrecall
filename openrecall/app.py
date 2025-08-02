from threading import Thread

from flask import Flask, render_template_string, request, send_from_directory, send_file
from jinja2 import BaseLoader
from io import BytesIO
import json
from PIL import Image, ImageDraw

from openrecall.config import appdata_folder, screenshots_path
from openrecall.database import create_db, get_sorted_entries, get_all_entries, get_connection, get_cursor, scheme
from openrecall.nlp import get_embedding
from openrecall.screenshot import record_screenshots_thread
from openrecall.utils import human_readable_time, timestamp_to_human_readable
import os

app = Flask(__name__)

app.jinja_env.filters["human_readable_time"] = human_readable_time
app.jinja_env.filters["timestamp_to_human_readable"] = timestamp_to_human_readable

base_template = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>OpenRecall</title>
  <!-- Bootstrap CSS -->
  <link href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css" rel="stylesheet">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.3.0/font/bootstrap-icons.css">
  <style>
    .slider-container {
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 20px;
    }
    .slider {
      width: 80%;
    }
    .slider-value {
      margin-top: 10px;
      font-size: 1.2em;
    }
    .image-container {
      margin-top: 20px;
      text-align: center;
    }
    .image-container img {
      max-width: 100%;
      height: auto;
    }
  </style>
</head>
<body>
<nav class="navbar navbar-light bg-light">
  <div class="container">
    <form class="form-inline my-2 my-lg-0 w-100 d-flex" action="/search" method="get">
      <input class="form-control flex-grow-1 mr-sm-2" type="search" name="q" placeholder="Search" aria-label="Search">
      <button class="btn btn-outline-secondary my-2 my-sm-0" type="submit">
        <i class="bi bi-search"></i>
      </button>
    </form>
  </div>
</nav>
{% block content %}

{% endblock %}

  <!-- Bootstrap and jQuery JS -->
  <script src="https://code.jquery.com/jquery-3.5.1.slim.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/@popperjs/core@2.5.3/dist/umd/popper.min.js"></script>
  <script src="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/js/bootstrap.min.js"></script>
  
</body>
</html>
"""


class StringLoader(BaseLoader):
    def get_source(self, environment, template):
        if template == "base_template":
            return base_template, None, lambda: True
        return None, None, None


app.jinja_env.loader = StringLoader()


@app.route("/")
def timeline():
    entries = get_all_entries()
    entries = [{k: v for k, v in e._asdict().items() if k not in ["embedding", "ocr_data"]} for e in entries]
    return render_template_string(
        """
{% extends "base_template" %}
{% block content %}
{% if entries|length > 0 %}
  <div class="container">
    <div class="slider-container">
      <input type="range" class="slider custom-range" id="discreteSlider" min="0" max="{{entries|length - 1}}" step="1" value="{{entries|length - 1}}">
      <div class="slider-value" id="sliderValue">{{entries[0].timestamp | timestamp_to_human_readable }}</div>
    </div>
    <div class="image-container">
      <img id="timestampImage" src="/static/{{entries[0].filename}}" alt="Image for timestamp">
    </div>
  </div>
  <script>
    const entries = {{ entries | tojson }};
    const slider = document.getElementById('discreteSlider');
    const sliderValue = document.getElementById('sliderValue');
    const timestampImage = document.getElementById('timestampImage');

    slider.addEventListener('input', function() {
      const reversedIndex = entries.length - 1 - slider.value;
      const entry = entries[reversedIndex];
      sliderValue.textContent = new Date(entry.timestamp * 1000).toLocaleString();
      timestampImage.src = `/static/${entry.filename}`;
    });

    // Initialize
    slider.value = entries.length - 1;
    const initialEntry = entries[0];
    sliderValue.textContent = new Date(initialEntry.timestamp * 1000).toLocaleString();
    timestampImage.src = `/static/${initialEntry.filename}`;
  </script>
{% else %}
  <div class="container">
      <div class="alert alert-info" role="alert">
          Nothing recorded yet, wait a few seconds.
      </div>
  </div>
{% endif %}
{% endblock %}
""",
        entries=entries,
    )


@app.route("/search")
def search():
    q = request.args.get("q")
    query_embedding = get_embedding(q)
    sorted_entries = get_sorted_entries(query_embedding, top_k=100)
    sorted_entries = [{k: v for k, v in e._asdict().items() if k not in ["embedding", "ocr_data"]} for e in sorted_entries]
    return render_template_string(
        """
{% extends "base_template" %}
{% block content %}
    <div class="container">
        <div class="row">
            {% for entry in entries %}
                <div class="col-md-3 mb-4">
                    <div class="card">
                        <a href="#" data-toggle="modal" data-target="#modal-{{ loop.index0 }}">
                            <img src="/highlighted/{{ entry.filename }}?q={{ q | urlencode }}" alt="Image" class="card-img-top">
                        </a>
                    </div>
                </div>
                <div class="modal fade" id="modal-{{ loop.index0 }}" tabindex="-1" role="dialog" aria-labelledby="exampleModalLabel" aria-hidden="true">
                    <div class="modal-dialog modal-xl" role="document" style="max-width: none; width: 100vw; height: 100vh; padding: 20px;">
                        <div class="modal-content" style="height: calc(100vh - 40px); width: calc(100vw - 40px); padding: 0;">
                            <div class="modal-body" style="padding: 0;">
                                <img src="/highlighted/{{ entry.filename }}?q={{ q | urlencode }}" alt="Image" style="width: 100%; height: 100%; object-fit: contain; margin: 0 auto;">
                            </div>
                        </div>
                    </div>
                </div>
            {% endfor %}
        </div>
    </div>
{% endblock %}
""",
        entries=sorted_entries,
        q=q
    )


@app.route("/static/<filename>")
def serve_image(filename):
    return send_from_directory(screenshots_path, filename)


@app.route("/highlighted/<filename>")
def serve_highlighted(filename):
    q = request.args.get("q")
    if not q:
        return serve_image(filename)
    conn = get_connection()
    cursor = get_cursor(conn)
    if scheme == "sqlite":
        cursor.execute("SELECT ocr_data FROM entries WHERE filename = ?", (filename,))
    else:
        cursor.execute("SELECT ocr_data FROM entries WHERE filename = %s", (filename,))
    row = cursor.fetchone()
    ocr_json = row["ocr_data"] if row else None
    conn.close()
    if not ocr_json:
        return serve_image(filename)
    ocr_data = json.loads(ocr_json)
    image_path = os.path.join(screenshots_path, filename)
    if not os.path.exists(image_path):
        return "Image not found", 404
    img = Image.open(image_path)
    draw = ImageDraw.Draw(img)
    width, height = img.size
    query_words = set(w.lower() for w in q.split())
    for page in ocr_data.get('pages', []):
        ph, pw = page['dimensions']
        # Assuming dimensions match, but to be safe
        for block in page.get('blocks', []):
            for line in block.get('lines', []):
                for word in line.get('words', []):
                    if word['value'].lower() in query_words:
                        (x1, y1), (x2, y2) = word['geometry']
                        bbox = (x1 * width, y1 * height, x2 * width, y2 * height)
                        draw.rectangle(bbox, outline="red", width=3)
    img_io = BytesIO()
    img.save(img_io, 'WEBP', lossless=True)
    img_io.seek(0)
    return send_file(img_io, mimetype='image/webp')


if __name__ == "__main__":
    create_db()

    print(f"Appdata folder: {appdata_folder}")

    # Start the thread to record screenshots
    t = Thread(target=record_screenshots_thread)
    t.start()

    app.run(port=8082)
