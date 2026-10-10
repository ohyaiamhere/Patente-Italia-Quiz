# Quiz Patente B (IT / FR / DE)

Local True/False quiz for the Italian driving-licence theory exam (**Patente B**), with French and German translations of the question text.

Questions come from the Anki deck `Quiz_esame_Patente_B_Italia_20182019.apkg`.

## Features

- Languages: **Italiano**, **Français**, **Deutsch**
- UI chrome is always Italian; only the *question text* is translated
- **Italiano**: Italian only
- **Français / Deutsch**: Italian plus the selected language
- Random draws from the full bank (~7 000 questions)
- Image when available; **V** / **F** answers
- Streak on correct answers; short Italian explanation when wrong
- **Precedente / Successiva**: step back through answered questions (server stores only id + answer; text is reloaded from the bank)
- Runs as a local Flask app in the browser (Pydroid, Termux, or desktop)

## Requirements

```bash
pip install -r requirements.txt
```

**Termux** (optional packages):

```bash
pkg update
pkg install python
pip install flask
# optional: open the browser from Termux
pkg install termux-api
```

**Pydroid**: install Flask from the Pip menu if needed, then run `app.py` from this folder.

## Run

```bash
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

Termux helper:

```bash
termux-open-url http://127.0.0.1:5000
```

### Android tips (Pydroid)

Android may suspend or kill a background Python process. Prefer **split-screen** (Pydroid console + browser) so the process stays in the foreground. The app may try a partial wake lock when the platform allows it (`ANDROID_WAKELOCK=0` to skip).

### Environment variables

| Variable           | Default     | Meaning                                         |
|--------------------|-------------|-------------------------------------------------|
| `HOST`             | `127.0.0.1` | Bind address                                    |
| `PORT`             | `5000`      | Port                                            |
| `PATENTE_SECRET`   | *(fixed)*   | Flask session secret (override if you like)     |
| `ANDROID_WAKELOCK` | `1`         | Set `0` to skip Android wake-lock attempts      |

## Project layout

```text
app.py                 Flask quiz app + embedded UI
requirements.txt
data/
  questions_it.json    Italian bank
  questions_fr.json    French translations
  questions_de.json    German translations
  cache_fr.json        Translation resume cache (scripts)
  cache_de.json
media/                 Quiz images (PNG)
scripts/
  export_from_anki.py  Deck → JSON + media
  translate_questions.py  Build FR/DE from Italian
```

## Question format

**Italian bank** (`data/questions_it.json`):

```json
{
  "id": 1552906424669,
  "question": "…",
  "answer": "V",
  "image": "550.png"
}
```

**Translations** (`data/questions_fr.json`, `data/questions_de.json`) store only what the app needs:

```json
{
  "id": 1552906424669,
  "question": "…"
}
```

- `answer` is `V` (true) or `F` (false), as in the exam (Italian bank only)
- The Anki deck has no long explanations; the app generates a short Italian line when the user is wrong
- A few items reference `564.png`, which is missing from the package; the UI shows a “no image” placeholder

## Re-export from Anki

After replacing the `.apkg`:

```bash
python scripts/export_from_anki.py
python scripts/translate_questions.py
```

Translation uses `deep-translator` (Google Translate) and is resumable via `data/cache_de.json` / `data/cache_fr.json` (optional; only for re-translation):

```bash
pip install -r requirements-dev.txt
```
