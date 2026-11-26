from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    abort,
)

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from database import get_db_connection, initialize_database

from dotenv import load_dotenv
from supabase import create_client

import os
import uuid
from datetime import datetime

# ---------------------------------------------------------
# APP AND SUPABASE CONFIGURATION
# ---------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)
app.config["MAX_CONTENT_LENGTH"] = 55 * 1024 * 1024

MAX_IMAGE_SIZE = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
STORAGE_BUCKET = "vehicle-photos"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing from .env")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Keep the local folder only for compatibility with the existing project.
# New uploaded photos are stored in Supabase Storage, not on the local disk.
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

initialize_database()


# ---------------------------------------------------------
# IMAGE / SUPABASE HELPERS
# ---------------------------------------------------------


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_storage_filenames():
    """Return the filenames currently present in the public Storage bucket."""
    try:
        objects = supabase.storage.from_(STORAGE_BUCKET).list()
        return {
            item.get("name")
            for item in objects
            if isinstance(item, dict) and item.get("name")
        }
    except Exception:
        # Do not crash the whole website if Storage listing is temporarily down.
        # An empty set means image entries are hidden until Storage is reachable.
        app.logger.exception("Could not list Supabase Storage files.")
        return set()


def get_public_image_url(filename):
    """Build the public URL for a file stored in the vehicle-photos bucket."""
    if not filename:
        return None

    safe_filename = os.path.basename(filename)
    result = supabase.storage.from_(STORAGE_BUCKET).get_public_url(safe_filename)

    # Supabase Python SDK versions normally return a string. This also handles
    # SDK versions that return an object containing a public URL.
    if isinstance(result, str):
        return result

    if isinstance(result, dict):
        return result.get("publicUrl") or result.get("public_url")

    return getattr(result, "public_url", None)


def attach_public_image_urls(images, available_files):
    """Add image_url and omit DB records whose files are absent from Storage."""
    ready_images = []

    for image in images:
        item = dict(image)
        filename = item.get("image_filename")

        if not filename or os.path.basename(filename) not in available_files:
            continue

        item["image_url"] = get_public_image_url(filename)

        if item["image_url"]:
            ready_images.append(item)

    return ready_images


def delete_unused_image_files(filenames):
    """Delete Storage objects only when no database record still uses them."""
    connection = get_db_connection()

    try:
        for filename in set(filenames):
            if not filename:
                continue

            safe_filename = os.path.basename(filename)

            used = connection.execute(
                """
                SELECT 1
                FROM vehicles
                WHERE image_filename = ?

                UNION

                SELECT 1
                FROM vehicle_images
                WHERE image_filename = ?

                LIMIT 1
                """,
                (safe_filename, safe_filename),
            ).fetchone()

            if not used:
                try:
                    supabase.storage.from_(STORAGE_BUCKET).remove([safe_filename])
                except Exception:
                    app.logger.exception(
                        "Could not delete Storage image: %s", safe_filename
                    )

    finally:
        connection.close()


def upload_image_to_storage(image, filename):
    """Upload one image to Supabase Storage."""
    content_type = image.mimetype or "application/octet-stream"
    image.stream.seek(0)
    image_bytes = image.read()

    supabase.storage.from_(STORAGE_BUCKET).upload(
        path=filename,
        file=image_bytes,
        file_options={
            "content-type": content_type,
            "upsert": "false",
        },
    )


# ---------------------------------------------------------
# ADMIN SECURITY
# ---------------------------------------------------------


def admin_required():
    if not session.get("user_id"):
        return redirect(url_for("admin_login"))

    if session.get("role") != "admin":
        abort(403)

    return None


# ---------------------------------------------------------
# HOME PAGE - APPROVED ADS ONLY
# ---------------------------------------------------------


@app.route("/")
def home():
    connection = get_db_connection()

    try:
        rows = connection.execute("""
            SELECT *
            FROM vehicles
            WHERE status = 'approved'
            ORDER BY submitted_at DESC
            """).fetchall()

        cars = []
        bikes = []
        available_files = get_storage_filenames()

        for row in rows:
            vehicle = dict(row)

            images = connection.execute(
                """
                SELECT image_filename
                FROM vehicle_images
                WHERE vehicle_id = ?
                ORDER BY id ASC
                """,
                (vehicle["id"],),
            ).fetchall()

            image_rows = [dict(image) for image in images]

            if not image_rows and vehicle.get("image_filename"):
                image_rows = [{"image_filename": vehicle["image_filename"]}]

            vehicle["images"] = attach_public_image_urls(image_rows, available_files)

            if vehicle["vehicle_type"] == "Car":
                cars.append(vehicle)
            elif vehicle["vehicle_type"] == "Bike":
                bikes.append(vehicle)

        return render_template("index.html", cars=cars, bikes=bikes)

    finally:
        connection.close()


# ---------------------------------------------------------
# REGISTER
# ---------------------------------------------------------


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("home"))

    message = ""

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not email or not password:
            message = "Please fill in all fields."
        elif len(password) < 8:
            message = "Password must contain at least 8 characters."
        else:
            connection = get_db_connection()

            try:
                connection.execute(
                    """
                    INSERT INTO users
                    (username, email, password_hash)
                    VALUES (?, ?, ?)
                    """,
                    (
                        username,
                        email,
                        generate_password_hash(password),
                    ),
                )
                connection.commit()
                return redirect(url_for("login"))

            except Exception:
                connection.rollback()
                message = "This username or email may already be registered."

            finally:
                connection.close()

    return render_template("register.html", message=message)


# ---------------------------------------------------------
# NORMAL USER LOGIN
# ---------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("home"))

    message = ""

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        connection = get_db_connection()
        try:
            user = connection.execute(
                "SELECT * FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        finally:
            connection.close()

        if user and check_password_hash(user["password_hash"], password):
            if user["role"] == "admin":
                message = "Please use the Admin Login page."
            else:
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["role"] = user["role"]
                return redirect(url_for("home"))
        else:
            message = "Incorrect email or password."

    return render_template("login.html", message=message)


# ---------------------------------------------------------
# ADMIN LOGIN
# ---------------------------------------------------------


@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():
    if session.get("user_id"):
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        session.clear()

    message = ""

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        connection = get_db_connection()
        try:
            user = connection.execute(
                "SELECT * FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        finally:
            connection.close()

        if user and check_password_hash(user["password_hash"], password):
            if user["role"] == "admin":
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["role"] = "admin"
                return redirect(url_for("admin_dashboard"))
            message = "This account does not have admin permission."
        else:
            message = "Incorrect admin email or password."

    return render_template("admin_login.html", message=message)


# ---------------------------------------------------------
# LOGOUT
# ---------------------------------------------------------


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ---------------------------------------------------------
# SELL VEHICLE PAGE
# ---------------------------------------------------------


@app.route("/sell")
def sell_vehicle():
    if not session.get("user_id"):
        return redirect(url_for("login"))

    if session.get("role") == "admin":
        abort(403)

    return render_template("vehicle_form.html")


# ---------------------------------------------------------
# SUBMIT VEHICLE - PHOTOS GO TO SUPABASE STORAGE
# ---------------------------------------------------------


@app.route("/submit_vehicle", methods=["POST"])
def submit_vehicle():
    if not session.get("user_id"):
        return redirect(url_for("login"))

    if session.get("role") == "admin":
        abort(403)

    vehicle_type = request.form.get("vehicle_type", "")
    vehicle_name = request.form.get("vehicle_name", "").strip()
    year_text = request.form.get("year", "").strip()
    price_text = request.form.get("price", "").strip()
    location = request.form.get("location", "").strip()
    description = request.form.get("description", "").strip()

    images = [
        image
        for image in request.files.getlist("vehicle_images")
        if image and image.filename
    ]

    if not vehicle_name or not price_text or not description:
        return render_template(
            "vehicle_form.html",
            message="Please fill in all required fields.",
        )

    if vehicle_type not in ["Car", "Bike"]:
        return render_template(
            "vehicle_form.html",
            message="Please select Car or Bike.",
        )

    try:
        price = float(price_text)
        if price <= 0:
            raise ValueError
    except ValueError:
        return render_template(
            "vehicle_form.html",
            message="Please enter a valid price.",
        )

    year = None
    if year_text:
        try:
            year = int(year_text)
            if year < 1900 or year > datetime.now().year:
                raise ValueError
        except ValueError:
            return render_template(
                "vehicle_form.html",
                message="Please enter a valid vehicle year.",
            )

    if len(description.split()) > 200:
        return render_template(
            "vehicle_form.html",
            message="Description must not exceed 200 words.",
        )

    if not 1 <= len(images) <= 5:
        return render_template(
            "vehicle_form.html",
            message="Please upload between 1 and 5 photos.",
        )

    for image in images:
        image.stream.seek(0, os.SEEK_END)
        image_size = image.stream.tell()
        image.stream.seek(0)

        if image_size > MAX_IMAGE_SIZE:
            return render_template(
                "vehicle_form.html",
                message=(
                    "Image Upload Failed! Each photo must be 10 MB or less. "
                    "Please select a smaller image."
                ),
            )

        if not allowed_file(image.filename):
            return render_template(
                "vehicle_form.html",
                message="Only JPG, JPEG, PNG and WEBP photos are allowed.",
            )

    uploaded_files = []
    connection = None

    try:
        # Upload photos to Supabase Storage first.
        for image in images:
            extension = image.filename.rsplit(".", 1)[1].lower()
            filename = secure_filename(str(uuid.uuid4()) + "." + extension)
            upload_image_to_storage(image, filename)
            uploaded_files.append(filename)

        connection = get_db_connection()
        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO vehicles (
                user_id, vehicle_type, vehicle_name, year,
                price, location, description, image_filename, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            RETURNING id
            """,
            (
                session["user_id"],
                vehicle_type,
                vehicle_name,
                year,
                price,
                location,
                description,
                uploaded_files[0],
            ),
        )

        vehicle_id = cursor.fetchone()["id"]

        for filename in uploaded_files:
            cursor.execute(
                """
                INSERT INTO vehicle_images
                (vehicle_id, image_filename)
                VALUES (?, ?)
                """,
                (vehicle_id, filename),
            )

        connection.commit()

        flash(
            "Your advertisement was submitted successfully. "
            "It is waiting for admin approval.",
            "success",
        )
        return redirect(url_for("home"))

    except Exception:
        app.logger.exception("Vehicle submission failed.")

        if connection:
            connection.rollback()

        # Remove files uploaded for this failed submission.
        if uploaded_files:
            try:
                supabase.storage.from_(STORAGE_BUCKET).remove(uploaded_files)
            except Exception:
                app.logger.exception("Could not clean up uploaded Storage files.")

        return render_template(
            "vehicle_form.html",
            message="Something went wrong. Please try again.",
        )

    finally:
        if connection:
            connection.close()


# ---------------------------------------------------------
# USER - MY ADVERTISEMENTS
# ---------------------------------------------------------


@app.route("/my-ads")
def my_ads():
    if not session.get("user_id"):
        return redirect(url_for("login"))

    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))

    connection = get_db_connection()

    try:
        rows = connection.execute(
            """
            SELECT *
            FROM vehicles
            WHERE user_id = ?
            ORDER BY submitted_at DESC
            """,
            (session["user_id"],),
        ).fetchall()

        vehicles = []
        available_files = get_storage_filenames()

        for row in rows:
            vehicle = dict(row)

            images = connection.execute(
                """
                SELECT id, image_filename
                FROM vehicle_images
                WHERE vehicle_id = ?
                ORDER BY id ASC
                """,
                (vehicle["id"],),
            ).fetchall()

            image_rows = [dict(image) for image in images]

            if not image_rows and vehicle.get("image_filename"):
                image_rows = [
                    {
                        "id": None,
                        "image_filename": vehicle["image_filename"],
                    }
                ]

            vehicle["images"] = attach_public_image_urls(image_rows, available_files)
            vehicles.append(vehicle)

    finally:
        connection.close()

    return render_template("my_ads.html", vehicles=vehicles)


# ---------------------------------------------------------
# USER - DELETE OWN ADVERTISEMENT
# ---------------------------------------------------------


@app.route("/my-ads/delete/<int:vehicle_id>", methods=["POST"])
def user_delete_vehicle(vehicle_id):
    if not session.get("user_id"):
        return redirect(url_for("login"))

    if session.get("role") == "admin":
        abort(403)

    connection = get_db_connection()
    image_filenames = []

    try:
        vehicle = connection.execute(
            """
            SELECT id, image_filename
            FROM vehicles
            WHERE id = ? AND user_id = ?
            """,
            (vehicle_id, session["user_id"]),
        ).fetchone()

        if vehicle is None:
            flash(
                "Advertisement not found or you do not have permission.",
                "error",
            )
            return redirect(url_for("my_ads"))

        if vehicle["image_filename"]:
            image_filenames.append(vehicle["image_filename"])

        images = connection.execute(
            """
            SELECT image_filename
            FROM vehicle_images
            WHERE vehicle_id = ?
            """,
            (vehicle_id,),
        ).fetchall()

        for image in images:
            if image["image_filename"]:
                image_filenames.append(image["image_filename"])

        connection.execute(
            "DELETE FROM vehicle_images WHERE vehicle_id = ?",
            (vehicle_id,),
        )
        connection.execute(
            """
            DELETE FROM vehicles
            WHERE id = ? AND user_id = ?
            """,
            (vehicle_id, session["user_id"]),
        )
        connection.commit()

    except Exception:
        connection.rollback()
        flash("Could not delete advertisement. Please try again.", "error")
        return redirect(url_for("my_ads"))

    finally:
        connection.close()

    delete_unused_image_files(image_filenames)
    flash("Your advertisement and its photos were deleted successfully.", "success")
    return redirect(url_for("my_ads"))


# ---------------------------------------------------------
# ADMIN DASHBOARD - ALL ADVERTISEMENTS
# ---------------------------------------------------------


@app.route("/admin")
def admin_dashboard():
    result = admin_required()
    if result:
        return result

    connection = get_db_connection()

    try:
        rows = connection.execute("""
            SELECT *
            FROM vehicles
            ORDER BY submitted_at DESC
            """).fetchall()

        vehicles = []
        available_files = get_storage_filenames()

        for row in rows:
            vehicle = dict(row)

            images = connection.execute(
                """
                SELECT id, image_filename
                FROM vehicle_images
                WHERE vehicle_id = ?
                ORDER BY id ASC
                """,
                (vehicle["id"],),
            ).fetchall()

            image_rows = [dict(image) for image in images]

            if not image_rows and vehicle.get("image_filename"):
                image_rows = [
                    {
                        "id": None,
                        "image_filename": vehicle["image_filename"],
                    }
                ]

            vehicle["images"] = attach_public_image_urls(image_rows, available_files)
            vehicles.append(vehicle)

    finally:
        connection.close()

    return render_template("admin_dashboard.html", vehicles=vehicles)


# ---------------------------------------------------------
# APPROVE VEHICLE
# ---------------------------------------------------------


@app.route("/admin/approve/<int:vehicle_id>", methods=["POST"])
def approve_vehicle(vehicle_id):
    result = admin_required()
    if result:
        return result

    connection = get_db_connection()

    try:
        cursor = connection.execute(
            """
            UPDATE vehicles
            SET status = 'approved',
                reviewed_at = CURRENT_TIMESTAMP,
                reviewed_by = ?
            WHERE id = ? AND status = 'pending'
            """,
            (session["user_id"], vehicle_id),
        )
        connection.commit()

        if cursor.rowcount == 1:
            flash("Advertisement approved successfully.", "success")
        else:
            flash("Advertisement not found or already reviewed.", "error")

    finally:
        connection.close()

    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------
# REJECT VEHICLE
# ---------------------------------------------------------


@app.route("/admin/reject/<int:vehicle_id>", methods=["POST"])
def reject_vehicle(vehicle_id):
    result = admin_required()
    if result:
        return result

    connection = get_db_connection()

    try:
        cursor = connection.execute(
            """
            UPDATE vehicles
            SET status = 'rejected',
                reviewed_at = CURRENT_TIMESTAMP,
                reviewed_by = ?
            WHERE id = ? AND status = 'pending'
            """,
            (session["user_id"], vehicle_id),
        )
        connection.commit()

        if cursor.rowcount == 1:
            flash("Advertisement rejected.", "success")
        else:
            flash("Advertisement not found or already reviewed.", "error")

    finally:
        connection.close()

    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------
# ADMIN - DELETE ANY ADVERTISEMENT
# ---------------------------------------------------------


@app.route("/admin/delete/<int:vehicle_id>", methods=["POST"])
def delete_vehicle(vehicle_id):
    result = admin_required()
    if result:
        return result

    connection = get_db_connection()
    image_filenames = []

    try:
        vehicle = connection.execute(
            """
            SELECT image_filename
            FROM vehicles
            WHERE id = ?
            """,
            (vehicle_id,),
        ).fetchone()

        if vehicle is None:
            flash("Advertisement not found.", "error")
            return redirect(url_for("admin_dashboard"))

        if vehicle["image_filename"]:
            image_filenames.append(vehicle["image_filename"])

        images = connection.execute(
            """
            SELECT image_filename
            FROM vehicle_images
            WHERE vehicle_id = ?
            """,
            (vehicle_id,),
        ).fetchall()

        for image in images:
            if image["image_filename"]:
                image_filenames.append(image["image_filename"])

        connection.execute(
            "DELETE FROM vehicle_images WHERE vehicle_id = ?",
            (vehicle_id,),
        )
        connection.execute(
            "DELETE FROM vehicles WHERE id = ?",
            (vehicle_id,),
        )
        connection.commit()

    except Exception:
        connection.rollback()
        flash("Could not delete advertisement. Please try again.", "error")
        return redirect(url_for("admin_dashboard"))

    finally:
        connection.close()

    delete_unused_image_files(image_filenames)
    flash("Advertisement and its photos deleted successfully.", "success")
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------
# ADMIN - DELETE ONE PHOTO
# ---------------------------------------------------------


@app.route("/admin/delete-image/<int:image_id>", methods=["POST"])
def delete_vehicle_image(image_id):
    result = admin_required()
    if result:
        return result

    connection = get_db_connection()
    filename_to_delete = None

    try:
        image = connection.execute(
            """
            SELECT id, vehicle_id, image_filename
            FROM vehicle_images
            WHERE id = ?
            """,
            (image_id,),
        ).fetchone()

        if image is None:
            flash("Photo not found.", "error")
            return redirect(url_for("admin_dashboard"))

        vehicle_id = image["vehicle_id"]
        filename_to_delete = image["image_filename"]

        vehicle = connection.execute(
            """
            SELECT id, image_filename
            FROM vehicles
            WHERE id = ?
            """,
            (vehicle_id,),
        ).fetchone()

        if vehicle is None:
            flash("Advertisement not found.", "error")
            return redirect(url_for("admin_dashboard"))

        remaining_images = connection.execute(
            """
            SELECT id, image_filename
            FROM vehicle_images
            WHERE vehicle_id = ?
            ORDER BY id ASC
            """,
            (vehicle_id,),
        ).fetchall()

        if len(remaining_images) <= 1:
            flash("At least one photo must remain.", "error")
            return redirect(url_for("admin_dashboard"))

        connection.execute(
            "DELETE FROM vehicle_images WHERE id = ?",
            (image_id,),
        )

        if vehicle["image_filename"] == filename_to_delete:
            next_image = next(
                (item for item in remaining_images if item["id"] != image_id),
                None,
            )

            if next_image:
                connection.execute(
                    """
                    UPDATE vehicles
                    SET image_filename = ?
                    WHERE id = ?
                    """,
                    (next_image["image_filename"], vehicle_id),
                )

        connection.commit()

    except Exception:
        connection.rollback()
        flash("Could not delete photo. Please try again.", "error")
        return redirect(url_for("admin_dashboard"))

    finally:
        connection.close()

    delete_unused_image_files([filename_to_delete])
    flash("Photo deleted successfully.", "success")
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------
# INFORMATION PAGES
# ---------------------------------------------------------


@app.route("/about")
def about():
    return render_template("info.html", title="About Us", page="about")


@app.route("/privacy")
def privacy():
    return render_template("info.html", title="Privacy Policy", page="privacy")


@app.route("/terms")
def terms():
    return render_template(
        "info.html",
        title="Terms and Conditions",
        page="terms",
    )


# ---------------------------------------------------------
# ERROR HANDLERS
# ---------------------------------------------------------


@app.errorhandler(403)
def access_denied(error):
    return "Access denied. Admin permission is required.", 403


@app.errorhandler(413)
def file_too_large(error):
    return (
        "Upload is too large. Please upload a maximum of 5 photos, "
        "each 10 MB or less.",
        413,
    )


if __name__ == "__main__":
    app.run(debug=True)
