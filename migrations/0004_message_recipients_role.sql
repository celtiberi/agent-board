ALTER TABLE mentions ADD COLUMN role TEXT NOT NULL DEFAULT 'to' CHECK (role IN ('to', 'cc'));
