#!/usr/bin/env python3
from copyright_alert import daily_workflow as dw
from copyright_alert import run_alert as ra

def main():
    dw.configure_region('BR')
    upc='046081232374'
    msg_id='bTN5QjVBa0NKM1hsRUxwWE4wbi9YQVYyaUlrPQ=='
    posted_id='om_x100b6b64ba185ca8e185ed6f10d09c5'
    subject='Infringement Claim: Spotify - 046081232374 - soundon'
    body, meta = ra.fetch_email(msg_id)
    ef = ra.extract_fields(body, subject, meta)
    ar = ra.batch_query_aeolus_by_upc([upc]).get(upc) or {}
    if (not ef.get('isrc') or ef.get('isrc') == 'N/A') and ar.get('isrc'):
        ef['isrc'] = str(ar.get('isrc')).strip() or 'N/A'
    ok = ra.append_tracker_row(ef, ar, posted_id, status='')
    print({'tracker_append_ok': ok, 'upc': upc, 'message_id': posted_id})
    return 0 if ok else 1

if __name__ == '__main__':
    raise SystemExit(main())
