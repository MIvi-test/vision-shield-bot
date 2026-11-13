"""Run the local bot with persistent PostgreSQL inbox."""

from backend.durable_polling import main

if __name__ == "__main__":
    main()
