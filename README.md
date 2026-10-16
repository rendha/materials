# Dehlsen Materials (separate website)

A quotation becomes a customer here. The **Project** person decides, for every material, *take from warehouse* or *purchase*
and sends the list to the **Store** person. The Store person keeps the warehouse stock, picks / buys and marks each item Done
or "not available". Both see the same sheet with the rates, and both can edit the rates.

## 1. Run it on your computer
```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python manage.py makemigrations store
python manage.py migrate
python manage.py setup_roles
python manage.py load_bos
python manage.py createsuperuser
python manage.py runserver
```
Open http://127.0.0.1:8000/admin/ , log in, create two users and put one in the **Project** group and the other in the **Store** group
(Users > the user > Groups). A superuser can do both jobs.

## 2. Connect it to the quotation website
On the **quotation website** (Render > quotation-app > Environment) add:
`MATERIALS_API_KEY` = a long random text (keep it secret)

On **this website** set the same text and the address:
```
QUOTATION_API_URL = https://<your-quotation-app>.onrender.com/api/materials/
QUOTATION_API_KEY = <the same long random text>
```
On your computer the easiest way is a file named `.env` next to `manage.py` (copy `.env.example` to `.env` and fill it in; no quotes needed). Or set them in PowerShell before `runserver`:
```
$env:QUOTATION_API_URL="http://127.0.0.1:8000/api/materials/"   # when the quotation site runs locally on another port, use that port
$env:QUOTATION_API_KEY="the-same-text"
```
(Run the two websites on different ports, e.g. `python manage.py runserver 8001`.)

## 3. Put it on Render (a second web service, its own database)
* New > PostgreSQL (name e.g. `materials-db`), copy its *Internal Database URL*.
* New > Web Service from the GitHub repo of this project.
  Build command: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`
  Start command: `gunicorn site_config.wsgi`
* Environment: `SECRET_KEY` (long random), `DEBUG=0`, `DATABASE_URL` (the Internal Database URL),
  `QUOTATION_API_URL`, `QUOTATION_API_KEY`.
* In the Render Shell: `python manage.py setup_roles`, `python manage.py load_bos` and `python manage.py createsuperuser`
  (or put them in the build command, see section 5: then no Shell is needed).
Do not touch the quotation database or `solar-cleaning-db`.

## 4. Install it as an app (PWA)
The website is installable: it has a manifest, a service worker (`/sw.js`), an offline page and icons (`store/static/store/`).
Open the Render address in Chrome on the phone > menu > **Install app** / **Add to Home screen** (iPhone: Safari > Share > Add to Home Screen).
It needs https, which Render gives. The app only stores the icons and the offline page: the sheets, stock and rates are always read from the
server, so nobody sees old numbers. Without internet it shows an "You are offline" page.
To change the icons, replace the PNG files and change `CACHE_VERSION` in `store/pwa.py`.

## 5. Put it on GitHub and Render, step by step
1. On your computer, in the project folder (cmd), once: `py -m venv venv`, `venv\Scripts\activate`, `pip install -r requirements.txt`,
   `py manage.py makemigrations store`, `py manage.py migrate`. This creates `store\migrations\0001_initial.py`, which MUST go to GitHub.
2. GitHub: new **private** repository (for example `materials`), empty (no README, no .gitignore). Then in the project folder:
```
git init
git add .
git status
git commit -m "Dehlsen materials site"
git branch -M main
git remote add origin https://github.com/<your-name>/materials.git
git push -u origin main
```
(`git status` must NOT list `.env`, `venv` or `db.sqlite3`; `.gitignore` skips them.)
3. Render > New > PostgreSQL: name `materials-db` (same region as the web service), copy its **Internal Database URL**.
4. Render > New > Web Service > the `materials` repository, branch `main`, runtime Python 3.
   Build command (this also creates the two roles, loads the BOS list once and creates the first admin; nothing is overwritten on later deploys):
```
pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate && python manage.py setup_roles && python manage.py load_bos && (python manage.py createsuperuser --noinput || true)
```
   Start command: `gunicorn site_config.wsgi`
5. Environment variables: `SECRET_KEY` (a long random text: `py -c "import secrets; print(secrets.token_urlsafe(50))"`), `DEBUG` = `0`,
   `DATABASE_URL` (the Internal Database URL), `QUOTATION_API_URL` = `https://quotation-app-t1qz.onrender.com/api/materials/`,
   `QUOTATION_API_KEY` (the same as `MATERIALS_API_KEY` on the quotation site), and for the first admin: `DJANGO_SUPERUSER_USERNAME`,
   `DJANGO_SUPERUSER_EMAIL`, `DJANGO_SUPERUSER_PASSWORD`.
6. When it is live, open the address, log in at `/admin/` with that admin, create the Project and the Store users (Users > the user > Groups), change the admin password.
7. Open the address on the phone and install it (section 4).
Updates later: change files, `git add .`, `git commit -m "..."`, `git push`: Render deploys by itself. If a model changed, run
`py manage.py makemigrations store` on your computer first and commit the new file in `store\migrations\`.

## A quotation that was not made in our quotation website
**New customer** > *Upload the quotation PDF* (or *Type the specifications in*). Only the specifications are read from the PDF: system kW, phase, panel
(brand, technology, watts, how many), inverter kW, structure (roof / GP / GI) and the DC / AC cable lengths. You check them on the next page; then the
materials site asks the quotation website (`/api/materials/estimate/`, same API key) to work the quotation out from them. The answer (the material list with
the quotation website's rates, the structure wage and the electrical work) becomes the customer, exactly like a confirmed quotation. The quotation website
must be deployed with the estimate address for this to work. Nothing is saved on the quotation website.
Rates for items that are in the warehouse are still the warehouse rates once the item is linked; everything else uses the quotation website's rate sheet.

## BOS, End cap, MC4, J bolt and labour
* **BOS** is one expandable line on the customer page. Open it to see and edit the 30 small items (quantity, rate, warehouse / purchase) and to add more.
  It shows the total of the items and the BOS amount of the quotation for comparison. Load / reset the list with `python manage.py load_bos` and edit it in /admin/ (Bos items).
* **J bolt** is 4 per panel: the quantity is 4 x the number of panels of the quotation.
* **End cap** and **MC4 connector** are not in the BOS. They are separate lines: type the make, quantity and rate, or pick one of the end caps / MC4 connectors that
  exist in the warehouse (the picker lists the warehouse items whose name contains "end cap" / "mc4", with how many are in stock; picking one fills in its make and rate).
  Name your warehouse items so they contain those words, for example "End cap 2x2" and "MC4 connector Ningbo".
* **Labour** shows the quotation's structure wage and electrical work. Choose *Company staff* or *Contract*; for a contract you can enter an advance, and the balance is shown.

After replacing the files, run again: `python manage.py makemigrations store`, `python manage.py migrate`. (`python manage.py load_bos --reset` loads the original BOS list again and discards your edits of it.)
For customers brought in earlier, run `python manage.py add_labour` to add the Labour rows (it reads the amounts from the quotation website, so `.env` must be set). Their lists keep the old layout; bring the quotation in again (delete the old customer in /admin/) to get the new layout.
