"""Initialise the database and optionally seed sample GPUs.

Usage:
    python -m scripts.init_db                # just create tables
    python -m scripts.init_db --seed-gpus 4  # create 4 sample GPUs
"""
import argparse

from app.database import Base, engine, session_scope
from app.models import Gpu


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-gpus", type=int, default=0, help="Add N sample GPUs if table is empty")
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)
    print("Tables created.")

    if args.seed_gpus > 0:
        with session_scope() as db:
            count = db.query(Gpu).count()
            if count > 0:
                print(f"GPUs table already has {count} rows; skipping seed.")
                return
            for i in range(1, args.seed_gpus + 1):
                db.add(Gpu(name=f"gpu-{i:02d}", model="RTX 4090", host=f"lab-node-{i}"))
            db.commit()
            print(f"Seeded {args.seed_gpus} GPUs.")


if __name__ == "__main__":
    main()
