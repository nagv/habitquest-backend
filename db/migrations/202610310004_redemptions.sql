-- Redemptions: a points balance can never go below zero.
-- A spending entry (negative amount) locks the child's row first, so two caregivers redeeming at once are
-- checked one after the other.

CREATE FUNCTION hq.points_never_negative() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.amount < 0 THEN
    PERFORM 1 FROM hq.children WHERE id = NEW.child_id FOR UPDATE;
    IF (SELECT coalesce(sum(amount), 0) FROM hq.points_ledger WHERE child_id = NEW.child_id) + NEW.amount < 0 THEN
      RAISE EXCEPTION 'not enough points' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER points_never_negative BEFORE INSERT ON hq.points_ledger
  FOR EACH ROW EXECUTE FUNCTION hq.points_never_negative();
