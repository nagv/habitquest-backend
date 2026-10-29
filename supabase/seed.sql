INSERT INTO hq.catalog VALUES
('bedtime-prep',1,true,'bedtime','Begin bedtime preparation','At your agreed cue, put on pajamas and brush your teeth.',6,13,10,'evening',1),
('morning-ready',1,true,'morning','Follow the morning checklist','At your agreed cue, get dressed and prepare for the day.',6,13,10,'morning',1),
('join-meal',1,true,'meals','Join the after-school meal','Come to the table at your agreed meal cue. You do not have to finish a prescribed amount.',6,13,5,'after_school',1),
('clear-plate',1,true,'responsibility','Clear your plate','After eating, put your plate in the agreed place.',6,13,2,'after_school',1),
('pack-bag',1,true,'responsibility','Prepare your school bag','Put the agreed school items in your bag for tomorrow.',6,13,5,'evening',1)
ON CONFLICT DO NOTHING;
INSERT INTO hq.chapters(id,world,category,title)
SELECT world||'-'||category,world,category,
CASE world WHEN 'space' THEN CASE category WHEN 'bedtime' THEN 'Prepare the Home Base' WHEN 'morning' THEN 'Launch the Day' WHEN 'meals' THEN 'Join the Crew' ELSE 'Prepare Your Equipment' END
WHEN 'forest' THEN CASE category WHEN 'bedtime' THEN 'Settle at the Treehouse' WHEN 'morning' THEN 'Start the Woodland Journey' WHEN 'meals' THEN 'Gather at the Table' ELSE 'Prepare Your Trail Pack' END
ELSE CASE category WHEN 'bedtime' THEN 'Settle at Home' WHEN 'morning' THEN 'Start the City Day' WHEN 'meals' THEN 'Join the Neighborhood Table' ELSE 'Prepare Your Day Bag' END END
FROM unnest(ARRAY['space','forest','city']) world CROSS JOIN unnest(ARRAY['bedtime','morning','meals','responsibility']) category
ON CONFLICT DO NOTHING;

-- Release 1: the development catalog migrated into the versioned model.
-- Development fixture only; not reviewed for a family pilot.
DO $$
DECLARE
  author uuid := '00000000-0000-0000-0000-0000000000a1';
  reviewer uuid := '00000000-0000-0000-0000-0000000000a2';
BEGIN
  IF EXISTS(SELECT 1 FROM hq.catalog_releases WHERE id = 1) THEN RETURN; END IF;

  INSERT INTO hq.goals(id, version, title, created_by) VALUES
    ('bedtime', 1, 'Build a consistent bedtime preparation routine', author),
    ('morning', 1, 'Be ready for the day with fewer reminders', author),
    ('bedtime-mornings', 1, 'Predictable bedtime and mornings', author),
    ('meals', 1, 'Join the after-school meal routine', author),
    ('responsibility', 1, 'Care for belongings and shared spaces', author);

  INSERT INTO hq.concerns(id, version, label, synonyms, created_by) VALUES
    ('bedtime-reminders', 1, 'Bedtime takes many reminders',
     ARRAY['won''t go to bed', 'stalls at bedtime', 'bedtime battles'], author),
    ('rushed-mornings', 1, 'Mornings are rushed',
     ARRAY['late for school', 'slow getting ready', 'morning stress'], author),
    ('meal-joining', 1, 'Hard to come to the table',
     ARRAY['won''t come to eat', 'skips the meal routine'], author),
    ('belongings-left', 1, 'Belongings and dishes are left around',
     ARRAY['plates left out', 'school things everywhere', 'messy shared spaces'], author);
  INSERT INTO hq.concern_goals VALUES
    ('bedtime-reminders', 1, 'bedtime', 1), ('bedtime-reminders', 1, 'bedtime-mornings', 0.6),
    ('rushed-mornings', 1, 'morning', 1), ('rushed-mornings', 1, 'bedtime-mornings', 0.6),
    ('meal-joining', 1, 'meals', 1),
    ('belongings-left', 1, 'responsibility', 1);

  INSERT INTO hq.habits VALUES ('bedtime-prep'), ('morning-ready'), ('join-meal'), ('clear-plate'), ('pack-bag');
  INSERT INTO hq.habit_versions(habit_id, version, title, instruction, min_age, max_age, routine_anchor, created_by)
  VALUES
    ('bedtime-prep', 1, 'Begin bedtime preparation',
     'At your agreed cue, put on pajamas and brush your teeth.', 6, 13, 'bedtime', author),
    ('morning-ready', 1, 'Follow the morning checklist',
     'At your agreed cue, get dressed and prepare for the day.', 6, 13, 'morning', author),
    ('join-meal', 1, 'Join the after-school meal',
     'Come to the table at your agreed meal cue. You do not have to finish a prescribed amount.',
     6, 13, 'after_school', author),
    ('clear-plate', 1, 'Clear your plate', 'After eating, put your plate in the agreed place.',
     6, 13, 'after_school', author),
    ('pack-bag', 1, 'Prepare your school bag', 'Put the agreed school items in your bag for tomorrow.',
     6, 13, 'evening', author);
  INSERT INTO hq.habit_levels(habit_id, version, level, minutes, points) VALUES
    ('bedtime-prep', 1, 'standard', 10, 1), ('morning-ready', 1, 'standard', 10, 1),
    ('join-meal', 1, 'standard', 5, 1), ('clear-plate', 1, 'standard', 2, 1), ('pack-bag', 1, 'standard', 5, 1);
  INSERT INTO hq.habit_goals VALUES
    ('bedtime-prep', 1, 'bedtime', 1), ('bedtime-prep', 1, 'bedtime-mornings', 0.8),
    ('morning-ready', 1, 'morning', 1), ('morning-ready', 1, 'bedtime-mornings', 0.8),
    ('join-meal', 1, 'meals', 1),
    ('clear-plate', 1, 'responsibility', 1), ('clear-plate', 1, 'meals', 0.5),
    ('pack-bag', 1, 'responsibility', 0.8), ('pack-bag', 1, 'morning', 0.5);
  INSERT INTO hq.habit_prereqs VALUES ('pack-bag', 1, 'morning-ready', 'prepares');
  INSERT INTO hq.habit_tags
  SELECT id, 1, 'access:short_instructions' FROM hq.habits
  UNION ALL VALUES ('bedtime-prep', 1, 'interest:space'), ('pack-bag', 1, 'interest:space');
  INSERT INTO hq.habit_variants VALUES
    ('bedtime-prep', 1, 'space', 'Prepare for Night Docking',
     'At the agreed cue, put on pajamas and brush your teeth.'),
    ('pack-bag', 1, 'space', 'Pack Your Mission Kit', 'Put the agreed school items in your bag for tomorrow.');

  INSERT INTO hq.chapter_templates(id, version, world, goal_id, title, created_by)
  SELECT world || '-' || goal, 1, world, goal, title, author FROM (VALUES
    ('space', 'bedtime', 'Prepare the Home Base'), ('space', 'morning', 'Launch the Day'),
    ('space', 'meals', 'Join the Crew'), ('space', 'responsibility', 'Prepare Your Equipment'),
    ('space', 'bedtime-mornings', 'Night Docking to Launch'),
    ('forest', 'bedtime', 'Settle at the Treehouse'), ('forest', 'morning', 'Start the Woodland Journey'),
    ('forest', 'meals', 'Gather at the Table'), ('forest', 'responsibility', 'Prepare Your Trail Pack'),
    ('forest', 'bedtime-mornings', 'Treehouse to Trail'),
    ('city', 'bedtime', 'Settle at Home'), ('city', 'morning', 'Start the City Day'),
    ('city', 'meals', 'Join the Neighborhood Table'), ('city', 'responsibility', 'Prepare Your Day Bag'),
    ('city', 'bedtime-mornings', 'Home to City Day')
  ) AS t(world, goal, title);

  UPDATE hq.goals SET status = 'approved', reviewed_by = reviewer, reviewed_at = now();
  UPDATE hq.concerns SET status = 'approved', reviewed_by = reviewer, reviewed_at = now();
  UPDATE hq.habit_versions SET status = 'approved', reviewed_by = reviewer, reviewed_at = now();
  UPDATE hq.chapter_templates SET status = 'approved', reviewed_by = reviewer, reviewed_at = now();

  INSERT INTO hq.catalog_releases(id, built_by, notes) VALUES (1, author, 'Development catalog migrated to releases');
  INSERT INTO hq.release_items
  SELECT 1, 'goal', id, version FROM hq.goals
  UNION ALL SELECT 1, 'concern', id, version FROM hq.concerns
  UNION ALL SELECT 1, 'habit', habit_id, version FROM hq.habit_versions
  UNION ALL SELECT 1, 'chapter', id, version FROM hq.chapter_templates;
  UPDATE hq.catalog_releases SET published_by = reviewer, published_at = now(), is_current = true WHERE id = 1;
END $$;
