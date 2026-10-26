# The Won't-Do List

I made this app to help me decide what needs attention, what can wait, and what I can let go. Paste in a task list and a model running on your computer sorts it into four groups:

- **Do now** — ignoring it has a cost soon, or it takes under ten minutes.
- **Do later** — it matters, but nothing breaks this week if it waits.
- **Let it expire** — nothing bad happens if it never gets done.
- **Not my problem** — someone else owns it, or I am waiting on them.

The app also checks dates, protects tasks involving money, health, safety, and commitments, and lets me change any decision.

## Run it

You need Python 3.9 or newer. The app uses the Python standard library and needs no package installation.

For model-based sorting, install [Ollama](https://ollama.com) and pull a model:

```sh
ollama pull gemma3:4b
```

Then start the app from this folder:

```sh
python -m wontdo
```

On Windows, run `start.bat`; on macOS or Linux, run `./start.sh`. Open <http://127.0.0.1:8742>. In **Settings**, use **Test connection** to check the model. The app can also run without a model using its keyword-based demo.

## Use it from the terminal

```sh
python -m wontdo sort examples/sample-tasks.txt --demo
python -m wontdo sort tasks.txt --format csv --budget 60
python -m wontdo sort tasks.txt --format ics
python -m wontdo doctor
```

Supported export formats are Markdown, CSV, JSON, and ICS. Run `python -m wontdo --help` for the full list of options.

## Data and privacy

The app listens on `127.0.0.1` and saves settings and history as JSON files in `data/`. Lists stay on this computer when using a local model. If I choose a model server on another machine, the app asks me to allow that first. There are no accounts, analytics, or cloud calls.

## Tests

Run the test suite from this folder:

```sh
python -m unittest discover -s tests -t .
```

The tests use a local mock model and do not need a real model or GPU. The browser tests run when Playwright and Chromium are installed.

## Contributing

Bug reports and pull requests are welcome. Please include the command you ran and the relevant test output when reporting a problem.

## Credits

The bundled Atkinson Hyperlegible fonts include their SIL Open Font License notices in `web/fonts/`.
