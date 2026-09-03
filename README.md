## AI-Powered-Cattle-Grazing-Route-Optimization-and-Pasture-Suitability-System

A web application that recommends grazing zones and travel routes for herders in the Adamawa Region, Cameroon, based on satellite-derived pasture suitability, carrying capacity, and cost-aware routing.

This README covers **how to set up and run the project locally.** For a full explanation of the project's purpose and methodology, see `Project_Documentation.docx`.

---

## Prerequisites

- **Python 3.9 – 3.12** (TensorFlow does not yet support Python 3.13+)
- The processed data package `data/processed/final/` (generated separately via the project's Colab notebook — see [Data Setup](#data-setup) below)
- A modern web browser (Chrome, Edge, or Firefox recommended)

---

## Setup Instructions

### 1. Clone the repository

```bash
git clone https://github.com/AchuchoNoel237/-AI-Powered-Cattle-Grazing-Route-Optimization-and-Pasture-Suitability-System.git
cd pasture_transhumance_project
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv
```

**Windows (PowerShell):**
```powershell
venv\Scripts\Activate.ps1
```

**macOS / Linux:**
```bash
source venv/bin/activate
```

You should see `(venv)` appear at the start of your terminal prompt.

> If PowerShell blocks the activation script, run this once first:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

### 3. Install dependencies

```bash
pip install django djangorestframework geopandas rasterio networkx pandas numpy scipy joblib tensorflow shapely pyproj
```

### 4. Data Setup

This project requires a processed data package that is **not included in this repository** (it is too large for version control). Obtain `pasture_project_data.zip` from the notebook and extract it so the folder structure looks like this:

```
pasture_transhumance_project/
└── data/
    └── processed/
        └── final/
            ├── manifest.json
            ├── parameters.json
            ├── ndvi/
            ├── chirps/
            ├── static/
            ├── biomass/
            ├── climatology/
            ├── suitability/
            ├── capacity/
            ├── routing/
            └── ml/
```

Verify it's in place:

```bash
python -c "import os; print(os.path.exists('data/processed/final/manifest.json'))"
```

This should print `True`.

### 5. Run database migrations

```bash
python manage.py migrate
```

### 6. Start the development server

```bash
python manage.py runserver
```

### 7. Open the app

Go to **`http://127.0.0.1:8000/`** in your browser.

> **GPS note:** browsers only allow location access on secure origins. If "Detect My Location" doesn't prompt for permission on `127.0.0.1`, try `http://localhost:8000/` instead — or simply tap your location on the map, which works either way.

---

## Troubleshooting

| Issue | Likely cause |
|---|---|
| `TemplateDoesNotExist: map.html` | The `templates/` folder is missing, misnamed, or not listed in `settings.py`'s `TEMPLATES['DIRS']` |
| `ModuleNotFoundError` on startup | A dependency didn't install — re-run the `pip install` command above inside the activated venv |
| Server starts but API calls fail | Confirm `data/processed/final/` is present and complete (see Data Setup) |
| GPS/location button does nothing | Try `localhost` instead of `127.0.0.1`, or use the map-click fallback |

---

## Stopping the server

Press `Ctrl+C` in the terminal where `runserver` is running.
