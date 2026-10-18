"""Local and Vercel entry point: export the Flask application as `app`."""

from powerpick import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=False)
