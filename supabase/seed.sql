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
