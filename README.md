# CEnT@HOME seat monitor

Checks CISIA's CEnT-S calendar every 20 minutes and **emails you when a CEnT@HOME
(from home, also called CENT@CASA) session has places available**.

Each alert email shows the **date**, the **university** and the **available places**,
plus the booking deadline and the booking link.

- **Cost: €0.** It runs on GitHub Actions (free for public repositories) and sends from your own Gmail.
- **Read-only.** It never logs in, books or pays anything.
- **No spam.** You get one email when places appear. If more places are added later you get another.
  If places go down or nothing changes, it stays silent.
- **Problems are reported.** If CISIA can't be read for about an hour (3 checks in a row), you get one
  "problem" email, and one "working again" email when it recovers.
- CEnT@UNI (in-person) sessions are ignored.

Page monitored: https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese
(The Italian version of the page also works.)

## Files

| File | What it is |
|---|---|
| `monitor.py` | The whole monitor, one file, Python standard library only (nothing to install) |
| `.github/workflows/monitor.yml` | Runs it every 20 minutes on GitHub, plus a manual "test-email" button |
| `state.json` | Created automatically. Remembers which places were already emailed. Only updated when something changes. |
| `tests/` | Automatic tests using a saved copy of the real page |

## Setup (about 10 minutes, once)

### 1. Create a Gmail app password

The monitor sends email from your Gmail with an *app password*: a separate 16-letter password
that only works for sending mail. Your normal password is never used.

1. Turn on 2-Step Verification for the Gmail account: https://myaccount.google.com/security
2. Open https://myaccount.google.com/apppasswords, name it `CEnT monitor` and click **Create**.
3. Copy the 16-letter password. Spaces don't matter.

### 2. Put the code on GitHub

Upload these files to your repository `CISIA_CENTS_BOT` on the `main` branch, keeping the folder
structure (including the hidden `.github` folder).

**Keep the repository Public** so GitHub Actions is free with no limits. No passwords or email
addresses are ever stored in the code. They live in encrypted Secrets.
If you want it **Private**, open `.github/workflows/monitor.yml` and change `*/20` to `*/30`.
That keeps you within GitHub's 2,000 free minutes per month.

### 3. Add the secrets

In the repository go to **Settings → Secrets and variables → Actions → New repository secret**
and add:

| Name | Value |
|---|---|
| `GMAIL_ADDRESS` | The Gmail address that sends the alerts, e.g. `you@gmail.com` |
| `GMAIL_APP_PASSWORD` | The 16-letter app password from step 1 |
| `EMAIL_TO` | *(optional)* Who receives alerts, separated by commas, e.g. `a@gmail.com,b@gmail.com`. If you leave it out, alerts go to `GMAIL_ADDRESS`. |

### 4. Turn it on and test

1. Open the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. Click **CEnT@HOME monitor → Run workflow**, choose **test-email**, and click **Run workflow**.
3. Within a minute you should get a "test email" listing today's CEnT@HOME sessions.
   If it lands in Spam, mark it **Not spam** so real alerts arrive in your inbox.

That's it. It now runs every 20 minutes by itself.

## When something goes wrong

Open **Actions → CEnT@HOME monitor** and click the latest run. The log shows what was checked and
the status of every CEnT@HOME session.

| What you see | Meaning / fix |
|---|---|
| `Email is not set up` | A secret is missing or misspelt. Check the names in step 3. |
| `Username and Password not accepted` | The app password is wrong or was deleted. Create a new one (step 1) and update `GMAIL_APP_PASSWORD`. |
| `Check failed (1 in a row)`, yellow warning | CISIA was briefly unreachable. Nothing to do; the next check usually works. |
| Run is red ❌ plus a "problem reading CISIA" email | 3+ failed checks in a row. Either the site is down or CISIA changed the page. Check the calendar link yourself. If the page looks different, the parser needs updating. |
| No runs for a while | GitHub sometimes delays scheduled runs by a few minutes at busy times; this is normal. GitHub also pauses schedules in a public repo after **60 days with no commits**. If that happens, Actions shows a banner: click **Enable workflow**. |

To check from your own computer (optional, needs Python 3.9+):

```bash
python3 monitor.py --show          # list CEnT@HOME sessions now, sends nothing
python3 -m unittest -v             # run the tests
```

## How it decides to email

| Situation | Email? |
|---|---|
| A CEnT@HOME session shows places available and wasn't available at the last check | ✅ Yes |
| An available session now shows **more** places than before | ✅ Yes |
| Places go down, or nothing changes | No |
| Sold out, then reopens later | ✅ Yes, again |
| CISIA shows "available" but no number | ✅ Yes, the email says the exact number isn't shown |
| The email fails to send | Retried at the next check |
| CISIA can't be read | Nothing is assumed about seats. After 3 failures in a row you get one problem email. |
