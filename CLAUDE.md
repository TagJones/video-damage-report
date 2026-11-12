# Video damage report

`tool/damage_report.py` turns a narrated walkthrough video into a damage report (see README.md).
Run it with the venv Python: `%USERPROFILE%\.damage-report\venv\Scripts\python.exe -I` on Windows,
`~/.damage-report/venv/bin/python -I` elsewhere. If the venv is missing, create it and `pip install -r requirements.txt`.
Outputs go to `<video stem> - damage report/` next to the video.

When asked to make a report from a video:

1. `transcribe "<video>"` (default model medium.en). Read `transcript.txt` and the word times in `transcript.json`
   (low `p` means an uncertain word, so tell the user). Then `overview "<workdir>"` and view `overview*.jpg` to see what is on camera when.
2. Rewrite `items.json` → `items` yourself. Don't trust the gap-based draft: one item per damage spot.
   `start`/`end` = when that spot is **on camera and being described**. People often point before or after
   speaking, so use the overview, not only the words. Don't let a window run into the next spot's footage.
   - `location`: short label from the user's own words ("Kitchen - countertop left of sink").
   - `description`: the user's words, lightly tidied (remove ums and false starts, fix punctuation).
     **Never add damage, sizes or detail that wasn't said.** If a word is unclear, keep it and mention it to the user.
   - Non-damage chatter → `"exclude": true` or drop it. Keep `video`, `info`, `video_path` as they are.
3. `frames "<workdir>" --redo`, then view every `frames/item_NN/contact.jpg` and set `photos` to the candidate
   that best shows the damage (finger pointing at it, in focus). The auto-pick is only the sharpest frame, and it can be
   the wrong spot. Two photos (pointing + clear view) are fine. Use `grab --item N m:ss` if none fit.
4. Ask the user for property address / tenant / landlord if `info` is blank, then `report "<workdir>"`.
5. Check the PDF visually before saying it's done.

Never commit videos or `* - damage report/` folders: they show the inside of someone's home.
