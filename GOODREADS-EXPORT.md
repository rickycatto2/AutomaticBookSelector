# Reviewed Goodreads exports

**Goodreads export** lists completed ABS/BookRamp books absent from the imported Goodreads read history. DNF, ignored, already exported and duplicate editions are excluded. The original Goodreads seed is retained. Previously imported to-read entries can supply a candidate edition/primary author; every export match still requires confirmation.

Review title, primary author, an ISBN known to Goodreads, the actual date completed, and optional **public** stars. Private feedback is never copied into this table and public stars never train recommendations. A matching print/ebook edition is fine. ISBN-10 leading zeros and X check digits are preserved, and ISBN-10/13 checksums are validated. Dates from ABS millisecond/ISO timestamps are converted to America/Chicago. Missing completion dates block exports until you enter the genuine date; a sync/import date is never substituted.

The CSV has `Title,Author,ISBN,My Rating,Date Read,Shelves`. Date Read is always present and nonblank, in YYYY-MM-DD format, and Shelves is always read. The generator quotes commas/newlines through the standard CSV writer and delivers UTF-8 with BOM from the browser. Goodreads may accept fields differently for existing reviews and rereads; one-book testing is still necessary. There is no supported guarantee that a CSV creates social-feed entries.

**Find Goodreads match** opens a title/primary-author web search. Paste a Goodreads book URL and press **Get ISBN from link** to retrieve structured ISBN metadata from the public book page. It is a bounded, explicit, rate-limited request, with no Goodreads credentials, redirects, proxy rotation or challenge bypass. Search crawling is not implemented because Goodreads' robots.txt disallows `/search`. Their page may deny automated lookup or have no ISBN (often a Kindle edition). In that case use Book details & editions, open a print edition and copy its ISBN manually. Fix narrator-filled ABS authors in the export form; this does not rewrite ABS metadata.

When you export, the batch CSV and its item records are saved atomically and the titles are marked **exported** immediately. Subsequent batches contain only new/failed titles. **Download again** reuses the saved batch if a browser download fails. Goodreads import confirmation is separate from export state.

Paste only Goodreads' failed-book list into **Save import result**. ISBN or title-plus-author can identify failed batch items; ambiguous generic errors ask you to select the failures. **Choose failed books manually** is always available. Failed items return to the review queue with confirmation cleared, requiring a corrected match before retry. Optional confirmation marks the remaining batch rows imported. Nothing is uploaded to Goodreads by this app.

## Gouged failed-file investigation

A test row for *Gouged* used 14 headers/14 values, valid audiobook ISBN `9798217356157`, a populated Date Read, and Shelves `read`. It was not missing a completion date and no CSV column shift was found. Goodreads' rejection does not establish an exact cause. An unrecognized audiobook edition is a plausible explanation.

The publisher lists a print edition of *Gouged: The End of a Fair Price—and What That Means for Your Wallet* by Lindsay Owens with ISBN **9798217058785**. The app's public-page lookup succeeded for https://www.goodreads.com/book/show/241504373 and retrieved Goodreads-listed ISBN **9781399633413**, the full title, and Lindsay Owens as primary author. This gives a Goodreads-listed identifier to try instead of an unrecognized audiobook ISBN. Review the completion date from ABS before exporting; it can differ from an earlier manually prepared file. CSV acceptance, challenge count and feed behavior still require a real manual import. Test CSVs and personal completion dates are kept outside the repository.

Sources checked 2026-10-09:

- Goodreads staff import guidance: https://help.goodreads.com/s/question/0D58V00007CGp99SAD/ive-read-how-to-add-my-amazon-books-to-goodreads-but-it-appears-i-need-to-select-each-book-individually-i-have-over-300-books-from-amazon-and-would-like-to-add-them-all-is-there-a-way-to-do-a-bulk-import-thanks
- Publisher ISBN: https://www.penguinrandomhouse.com/books/778487/gouged-by-lindsay-owens/
- Goodreads sample file supplied by the user: https://www.goodreads.com/assets/sample_export.csv
- Goodreads import page (requires sign-in): https://www.goodreads.com/review/import
- Goodreads robots: https://www.goodreads.com/robots.txt
