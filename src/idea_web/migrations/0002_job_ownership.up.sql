-- Driver versus attachment is durable queue state, not something inferred from mutable progress
-- JSON: a corrupt or rewritten progress document must never turn an attached job into a claimant
-- that can rotate, abandon or settle the run another job is driving.
ALTER TABLE jobs ADD COLUMN attached INTEGER NOT NULL DEFAULT 0;

UPDATE jobs SET attached = 1
WHERE json_valid(progress) AND json_extract(progress, '$.attached') = 1;

-- A subscriber the owner had already cancelled under 0001 was only flagged: 0001's
-- reconciliation would have mirrored the driver's result over it. From 0002 claims skip attached
-- rows and reconciliation skips cancel requests, so such a row would wait forever. It is detached
-- terminally here, exactly as 0002's request_cancel detaches one.
UPDATE jobs SET state = 'cancelled',
    progress = json_set(
        CASE WHEN json_valid(progress) THEN progress ELSE '{}' END, '$.detached', 1
    ),
    lease_owner = NULL, lease_until = NULL, heartbeat_at = NULL, claim_token = NULL
WHERE attached = 1 AND cancel_requested = 1 AND state IN ('intake', 'waiting', 'analysis');

-- ONE run id per local job, in the normalised column. A live local row written before durable run
-- ids takes its snapshot's id when it has one, otherwise a fresh one; the snapshot is then set to
-- the column's value, so both places agree and no reader ever needs the progress JSON for it.
UPDATE jobs SET run_id = CASE
        WHEN json_type(progress, '$.local.run_id') = 'text'
        THEN json_extract(progress, '$.local.run_id')
        ELSE lower(hex(randomblob(16)))
    END
WHERE run_id IS NULL AND state IN ('intake', 'waiting', 'analysis')
    AND CASE WHEN json_valid(progress) THEN json_type(progress, '$.local') END = 'object';

UPDATE jobs SET progress = json_set(progress, '$.local.run_id', run_id)
WHERE run_id IS NOT NULL
    AND CASE WHEN json_valid(progress) THEN json_type(progress, '$.local') END = 'object'
    AND COALESCE(
        CASE WHEN json_valid(progress) THEN json_extract(progress, '$.local.run_id') END, ''
    ) <> run_id;
