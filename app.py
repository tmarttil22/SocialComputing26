from flask import Flask, abort, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import sqlite3
import hashlib
import secrets
import re
import random
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = '123456789'
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, 'database.sqlite')

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None


@app.before_request
def ensure_co_creation_schema():
    if app.config.get('_co_creation_schema_ready'):
        return

    db = get_db()
    with db:
        db.execute('BEGIN IMMEDIATE')
        post_columns = {row['name'] for row in db.execute('PRAGMA table_info(posts)')}
        if 'coauthor_id' not in post_columns:
            db.execute('ALTER TABLE posts ADD COLUMN coauthor_id INTEGER')
        if 'coauthor_content' not in post_columns:
            db.execute('ALTER TABLE posts ADD COLUMN coauthor_content TEXT')
        user_columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
        if 'phone_number' not in user_columns:
            db.execute('ALTER TABLE users ADD COLUMN phone_number TEXT')
        db.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_number
            ON users (phone_number)
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS pending_signups (
                username TEXT PRIMARY KEY,
                password TEXT NOT NULL,
                location TEXT,
                birthdate DATE,
                profile TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS co_creation_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id INTEGER NOT NULL,
                coauthor_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                post_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                FOREIGN KEY (author_id) REFERENCES users(id),
                FOREIGN KEY (coauthor_id) REFERENCES users(id),
                FOREIGN KEY (post_id) REFERENCES posts(id)
            )
        ''')
    app.config['_co_creation_schema_ready'] = True


PHONE_NUMBER_PATTERN = re.compile(r'^\+[1-9]\d{6,14}$')


def require_verified_phone(user_id):
    user = query_db(
        'SELECT phone_number FROM users WHERE id = ?',
        (user_id,),
        one=True
    )
    if user and user['phone_number']:
        return None

    flash('Link and verify a unique phone number before posting or commenting.', 'warning')
    return redirect(url_for('verify_phone'))

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')


def is_new_user(created_at):
    if isinstance(created_at, str):
        created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
    return created_at is not None and (datetime.now() - created_at).days < 7


REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())

# Task 1, ch2 Design claim 3 implementation
def recent_interest_hashtags(user_id):
    authored_posts = query_db(
        'SELECT content FROM posts WHERE user_id = ? ORDER BY created_at DESC, id DESC',
        (user_id,)
    )
    interacted_posts = query_db('''
        SELECT p.content
        FROM posts p
        WHERE EXISTS (
            SELECT 1 FROM reactions r
            WHERE r.post_id = p.id AND r.user_id = ?
        )
        OR EXISTS (
            SELECT 1 FROM comments c
            WHERE c.post_id = p.id AND c.user_id = ?
        )
        ORDER BY p.created_at DESC, p.id DESC
    ''', (user_id, user_id))

    hashtags = []
    for posts in (authored_posts, interacted_posts):
        source_hashtags = []
        for post in posts:
            source_hashtags.extend(reversed(re.findall(r'(?<!\w)#\w+', post['content'])))
            if len(source_hashtags) >= 2:
                break
        hashtags.extend(source_hashtags[:2])
    return hashtags

# Task 1, ch2 Design claim 12 implementation
def popular_recent_hashtag(user_id):
    posts = query_db('''
        SELECT p.content, p.user_id, u.username
        FROM posts p
        JOIN users u ON u.id = p.user_id
        ORDER BY p.created_at DESC, p.id DESC
        LIMIT 20
    ''')

    hashtag_posters = {}
    for post in posts:
        if post['user_id'] == user_id:
            continue
        for hashtag in set(re.findall(r'(?<!\w)#\w+', post['content'])):
            hashtag_posters.setdefault(hashtag, {})[post['user_id']] = post['username']

    if not hashtag_posters:
        return None

    hashtag, posters = max(hashtag_posters.items(), key=lambda item: len(item[1]))
    if len(posters) < 2:
        return None
    return {
        'username': next(iter(posters.values())),
        'hashtag': hashtag,
        'count': len(posters),
    }


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    #  2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = """WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)
                          OR p.coauthor_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"""
        params.extend((current_user_id, current_user_id))

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.coauthor_id, p.coauthor_content, p.created_at,
                   u.username, u.id as user_id, u.created_at as user_created_at,
                   coauthor.username as coauthor_username, coauthor.created_at as coauthor_created_at,
                   NOT EXISTS (
                       SELECT 1 FROM posts previous
                       WHERE (previous.user_id = u.id OR previous.coauthor_id = u.id)
                         AND (previous.created_at < p.created_at
                              OR (previous.created_at = p.created_at AND previous.id < p.id))
                   ) AS is_first_author_post,
                   NOT EXISTS (
                       SELECT 1 FROM posts previous
                       WHERE (previous.user_id = coauthor.id OR previous.coauthor_id = coauthor.id)
                         AND (previous.created_at < p.created_at
                              OR (previous.created_at = p.created_at AND previous.id < p.id))
                   ) AS is_first_coauthor_post,
                   IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN users coauthor ON p.coauthor_id = coauthor.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.coauthor_id, p.coauthor_content, p.created_at,
                   u.username, u.id as user_id, u.created_at as user_created_at,
                   coauthor.username as coauthor_username, coauthor.created_at as coauthor_created_at,
                   NOT EXISTS (
                       SELECT 1 FROM posts previous
                       WHERE (previous.user_id = u.id OR previous.coauthor_id = u.id)
                         AND (previous.created_at < p.created_at
                              OR (previous.created_at = p.created_at AND previous.id < p.id))
                   ) AS is_first_author_post,
                   NOT EXISTS (
                       SELECT 1 FROM posts previous
                       WHERE (previous.user_id = coauthor.id OR previous.coauthor_id = coauthor.id)
                         AND (previous.created_at < p.created_at
                              OR (previous.created_at = p.created_at AND previous.id < p.id))
                   ) AS is_first_coauthor_post
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN users coauthor ON p.coauthor_id = coauthor.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    for post in posts:
        # Determine if the current user follows the main author
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('''
            SELECT c.id, c.content, c.created_at, u.username, u.id as user_id,
                   u.created_at as user_created_at
            FROM comments c
            JOIN users u ON c.user_id = u.id
            WHERE c.post_id = ?
            ORDER BY c.created_at ASC
        ''', (post['id'],))
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        post_dict['is_new_author'] = is_new_user(post_dict['user_created_at'])
        if post_dict.get('coauthor_content'):
            post_dict['coauthor_content'], _ = moderate_content(post_dict['coauthor_content'])
        post_dict['is_new_coauthor'] = is_new_user(post_dict.get('coauthor_created_at'))
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comment_dict['is_new_author'] = is_new_user(comment_dict['user_created_at'])
            comments_moderated.append(comment_dict)
        viewer_is_new_post_author = current_user_id is not None and (
            (post_dict['user_id'] == current_user_id and post_dict['is_new_author'])
            or (post_dict.get('coauthor_id') == current_user_id and post_dict['is_new_coauthor'])
        )
        if viewer_is_new_post_author:
            visible_comments = comments_moderated[-2:]
            for comment in visible_comments:
                if comment['user_id'] != current_user_id:
                    comment['show_first_comment_notice'] = True
                    break
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated
        })

    #  4. Render Template with Pagination Info 
    interest_hashtags = recent_interest_hashtags(current_user_id) if current_user_id else []
    popular_hashtag = popular_recent_hashtag(current_user_id) if current_user_id else None

    # Task 1, ch3 Design Claim 9 implementation
    coauthor_users = query_db(
        '''
        SELECT u.id, u.username
        FROM users u
        JOIN follows f ON f.followed_id = u.id
        WHERE f.follower_id = ?
        ORDER BY u.username
        ''',
        (current_user_id,)
    ) if current_user_id else []
    has_posted = bool(query_db(
        'SELECT 1 FROM posts WHERE user_id = ? OR coauthor_id = ? LIMIT 1',
        (current_user_id, current_user_id),
        one=True
    )) if current_user_id else False
    return render_template('feed.html.j2', 
                           posts=posts_data, 
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           suggested_hashtag=random.choice(interest_hashtags) if interest_hashtags else None,
                           popular_hashtag=popular_hashtag,
                           coauthor_users=coauthor_users,
                           has_posted=has_posted)

@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    phone_redirect = require_verified_phone(user_id)
    if phone_redirect:
        return phone_redirect

    # Get content from the submitted form
    content = request.form.get('content')
    coauthor_id_value = request.form.get('coauthor_id', '').strip()

    # Basic validation to ensure post is not empty
    if not content or not content.strip():
        flash('Post cannot be empty or was fully censored.', 'warning')
        return redirect(url_for('feed'))

    if coauthor_id_value:
        db = get_db()
        with db:
            db.execute(
                'INSERT INTO co_creation_requests (author_id, coauthor_id, content) VALUES (?, ?, ?)',
                (user_id, coauthor_id_value, content)
            )
        flash('Your post was sent for co-creation.', 'success')
    else:
        db = get_db()
        with db:
            db.execute('INSERT INTO posts (user_id, content) VALUES (?, ?)', (user_id, content))
        flash('Your post was successfully created!', 'success')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))


@app.route('/co-post-requests')
def co_post_requests():
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to view co-post requests.', 'danger')
        return redirect(url_for('login'))

    incoming_requests = query_db('''
        SELECT r.id, r.content, r.created_at, u.id AS author_id, u.username AS author_username
        FROM co_creation_requests r
        JOIN users u ON r.author_id = u.id
        WHERE r.coauthor_id = ? AND r.status = 'pending'
        ORDER BY r.created_at DESC, r.id DESC
    ''', (user_id,))
    return render_template(
        'co_post_requests.html.j2',
        incoming_requests=incoming_requests
    )


@app.route('/co-post-requests/<int:request_id>/complete', methods=['POST'])
def complete_co_post_request(request_id):
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to complete a co-post request.', 'danger')
        return redirect(url_for('login'))

    phone_redirect = require_verified_phone(user_id)
    if phone_redirect:
        return phone_redirect

    coauthor_content = request.form.get('coauthor_content', '')
    if not coauthor_content.strip():
        flash('Add your text before publishing the co-created post.', 'warning')
        return redirect(url_for('co_post_requests'))

    co_creation_request = query_db(
        '''SELECT id, author_id, coauthor_id, content
           FROM co_creation_requests
           WHERE id = ? AND coauthor_id = ? AND status = 'pending' ''',
        (request_id, user_id),
        one=True
    )
    if not co_creation_request:
        flash('This co-post request is no longer available.', 'danger')
        return redirect(url_for('co_post_requests'))

    db = get_db()
    with db:
        updated = db.execute(
            '''UPDATE co_creation_requests SET status = 'completed'
               WHERE id = ? AND coauthor_id = ? AND status = 'pending' ''',
            (request_id, user_id)
        )
        if updated.rowcount != 1:
            flash('This co-post request has already been completed.', 'warning')
            return redirect(url_for('co_post_requests'))
        post_cursor = db.execute(
            '''INSERT INTO posts (user_id, content, coauthor_id, coauthor_content)
               VALUES (?, ?, ?, ?)''',
            (
                co_creation_request['author_id'],
                co_creation_request['content'],
                user_id,
                coauthor_content
            )
        )
        db.execute(
            'UPDATE co_creation_requests SET post_id = ? WHERE id = ?',
            (post_cursor.lastrowid, request_id)
        )
    flash('The co-created post has been published.', 'success')
    return redirect(url_for('feed'))
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id, coauthor_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if user_id not in (post['user_id'], post['coauthor_id']):
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile') or '')
    user['profile'] = moderated_bio

    posts_raw = query_db(
        '''SELECT p.id, p.content, p.coauthor_id, p.coauthor_content, p.user_id, p.created_at,
                  author.username, author.created_at AS author_created_at,
                  coauthor.username AS coauthor_username, coauthor.created_at AS coauthor_created_at,
                  NOT EXISTS (
                      SELECT 1 FROM posts previous
                      WHERE (previous.user_id = author.id OR previous.coauthor_id = author.id)
                        AND (previous.created_at < p.created_at
                             OR (previous.created_at = p.created_at AND previous.id < p.id))
                  ) AS is_first_author_post,
                  NOT EXISTS (
                      SELECT 1 FROM posts previous
                      WHERE (previous.user_id = coauthor.id OR previous.coauthor_id = coauthor.id)
                        AND (previous.created_at < p.created_at
                             OR (previous.created_at = p.created_at AND previous.id < p.id))
                  ) AS is_first_coauthor_post
           FROM posts p
           JOIN users author ON author.id = p.user_id
           LEFT JOIN users coauthor ON coauthor.id = p.coauthor_id
           WHERE p.user_id = ? OR p.coauthor_id = ?
           ORDER BY p.created_at DESC''',
        (user['id'], user['id'])
    )
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        post['is_new_author'] = is_new_user(post['author_created_at'])
        if post['coauthor_content']:
            post['coauthor_content'], _ = moderate_content(post['coauthor_content'])
        post['is_new_coauthor'] = is_new_user(post['coauthor_created_at'])
        posts.append(post)

    user['is_new_member'] = is_new_user(user['created_at'])
    comments_raw = query_db('''
        SELECT c.id, c.content, c.user_id, c.post_id, c.created_at,
               u.username, u.created_at AS user_created_at
        FROM comments c
        JOIN users u ON u.id = c.user_id
        WHERE c.user_id = ?
        ORDER BY c.created_at DESC
        LIMIT 100
    ''', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comment['is_new_author'] = is_new_user(comment['user_created_at'])
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    reputation = calculate_reputation_score(user['id'], posts)

    return render_template('user_profile.html.j2', 
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           is_following=is_currently_following,
                           reputation=reputation)

def calculate_reputation_score(user_id, posts):
    rep_score = 0

    positive_reactions = ("haha", "like", "love", "wow")
    if user_risk_analysis(user_id) >= 4.0:
        return "LOW REPUTATION WARNING"

    post_ids = [post['id'] for post in posts]
    if  post_ids:
        post_placeholders = ','.join(['?'] * len(post_ids))
        p_reaction_placeholders = ','.join(['?'] * len(positive_reactions))

        # Add 2 score for each positive reaction got on the platform
        query = f"""
            SELECT COUNT(*) as count
            FROM reactions
            WHERE post_id IN ({post_placeholders})
            AND reaction_type IN ({p_reaction_placeholders})
        """

        params = tuple(post_ids) + tuple(positive_reactions)
        result = query_db(query, params, one = True)

        if result:
            rep_score += result['count'] * 3

        # Add 1 score for each post posted
        query = "SELECT COUNT(*) as count FROM posts WHERE user_id = ?"
        result = query_db(query, (user_id, ), one = True)
        if result:
            rep_score += result['count']
        
        # Add 4 score for each comment left on a post, including all users posts
        query = f"""
            SELECT COUNT(*) as count
            FROM comments
            WHERE post_id IN ({post_placeholders})
            AND user_id != ?
        """
        params = tuple(post_ids) + (user_id, )
        result = query_db(query, params, one = True)
        if result:
            rep_score += result['count'] * 4

    return rep_score

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.coauthor_id, p.coauthor_content, p.created_at,
               u.username, u.id as user_id, u.created_at as user_created_at,
               coauthor.username as coauthor_username, coauthor.created_at as coauthor_created_at,
               NOT EXISTS (
                   SELECT 1 FROM posts previous
                   WHERE (previous.user_id = u.id OR previous.coauthor_id = u.id)
                     AND (previous.created_at < p.created_at
                          OR (previous.created_at = p.created_at AND previous.id < p.id))
               ) AS is_first_author_post,
               NOT EXISTS (
                   SELECT 1 FROM posts previous
                   WHERE (previous.user_id = coauthor.id OR previous.coauthor_id = coauthor.id)
                     AND (previous.created_at < p.created_at
                          OR (previous.created_at = p.created_at AND previous.id < p.id))
               ) AS is_first_coauthor_post
        FROM posts p
        JOIN users u ON p.user_id = u.id
        LEFT JOIN users coauthor ON p.coauthor_id = coauthor.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content
    post['is_new_author'] = is_new_user(post['user_created_at'])
    if post['coauthor_content']:
        post['coauthor_content'], _ = moderate_content(post['coauthor_content'])
    post['is_new_coauthor'] = is_new_user(post['coauthor_created_at'])

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('''
        SELECT c.id, c.content, c.created_at, u.username, u.id as user_id,
               u.created_at as user_created_at
        FROM comments c
        JOIN users u ON c.user_id = u.id
        WHERE c.post_id = ?
        ORDER BY c.created_at ASC
    ''', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        print(comment['content'])
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comment['is_new_author'] = is_new_user(comment['user_created_at'])
        comments.append(comment)

    current_user_id = session.get('user_id')
    viewer_is_new_post_author = current_user_id is not None and (
        (post['user_id'] == current_user_id and post['is_new_author'])
        or (post.get('coauthor_id') == current_user_id and post['is_new_coauthor'])
    )
    if viewer_is_new_post_author:
        for comment in comments:
            if comment['user_id'] != current_user_id:
                comment['show_first_comment_notice'] = True
                break

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if session.get('pending_signup_username'):
        return redirect(url_for('verify_phone'))

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)
        existing_user = query_db(
            'SELECT id FROM users WHERE username = ?',
            (username,),
            one=True
        )
        if existing_user:
            flash('Username already taken. Please choose another one.', 'danger')
            return render_template('signup.html.j2')

        existing_pending_signup = query_db(
            'SELECT 1 FROM pending_signups WHERE username = ?',
            (username,),
            one=True
        )
        if existing_pending_signup:
            flash('A sign-up with that username is already awaiting phone verification.', 'danger')
            return render_template('signup.html.j2')

        db = get_db()
        try:
            with db:
                db.execute(
                    '''INSERT INTO pending_signups
                       (username, password, location, birthdate, profile)
                       VALUES (?, ?, ?, ?, ?)''',
                    (
                        username,
                        hashed_password,
                        location,
                        birthdate,
                        profile,
                    )
                )
        except sqlite3.IntegrityError:
            flash('A sign-up with that username is already awaiting phone verification.', 'danger')
            return render_template('signup.html.j2')
        session.clear()
        session['pending_signup_username'] = username
        return redirect(url_for('verify_phone'))

    return render_template('signup.html.j2')


@app.route('/verify-phone', methods=['GET', 'POST'])
def verify_phone():
    pending_signup_username = session.get('pending_signup_username')
    pending_signup = None
    if pending_signup_username:
        pending_signup = query_db(
            '''SELECT username, password, location, birthdate, profile
               FROM pending_signups WHERE username = ?''',
            (pending_signup_username,),
            one=True
        )
    user_id = session.get('user_id')
    if pending_signup_username and not pending_signup:
        session.pop('pending_signup_username', None)
        session.pop('phone_verification', None)
        flash('Your pending sign-up was not found. Please sign up again.', 'warning')
        return redirect(url_for('signup'))
    if not pending_signup and not user_id:
        flash('Sign up or log in before linking a phone number.', 'warning')
        return redirect(url_for('signup'))

    if user_id:
        user = query_db(
            'SELECT phone_number FROM users WHERE id = ?',
            (user_id,),
            one=True
        )
        if user and user['phone_number']:
            session.pop('phone_verification', None)
            flash('Your account already has a linked phone number.', 'info')
            return redirect(url_for('feed'))

    if request.method == 'POST':
        verification_step = request.form.get('step')
        if verification_step == 'change_number':
            session.pop('phone_verification', None)
        elif verification_step == 'send_code':
            session.pop('phone_verification', None)
            phone_number = request.form.get('phone_number', '').strip()
            if not PHONE_NUMBER_PATTERN.fullmatch(phone_number):
                flash('Enter a valid phone number in international format, such as +358401234567.', 'warning')
            else:
                if user_id:
                    existing_number = query_db(
                        'SELECT id FROM users WHERE phone_number = ? AND id != ?',
                        (phone_number, user_id),
                        one=True
                    )
                else:
                    existing_number = query_db(
                        'SELECT id FROM users WHERE phone_number = ?',
                        (phone_number,),
                        one=True
                    )

                if existing_number:
                    flash('That phone number is already linked to an account.', 'danger')
                else:
                    verification_code = f'{secrets.randbelow(1_000_000):06d}'
                    session['phone_verification'] = {
                        'phone_number': phone_number,
                        'code': verification_code,
                    }
                    flash('A confirmation code has been sent to your simulated phone.', 'info')
        elif verification_step == 'confirm_code':
            verification = session.get('phone_verification')
            submitted_code = request.form.get('confirmation_code', '')
            if not verification or not submitted_code.isdigit() or len(submitted_code) != 6:
                flash('Enter the six-digit confirmation code.', 'warning')
            elif not secrets.compare_digest(submitted_code, verification['code']):
                flash('The confirmation code is incorrect.', 'danger')
            else:
                phone_number = verification['phone_number']
                if pending_signup:
                    db = get_db()
                    try:
                        with db:
                            cursor = db.execute(
                                '''INSERT INTO users
                                   (username, password, location, birthdate, profile, phone_number)
                                   VALUES (?, ?, ?, ?, ?, ?)''',
                                (
                                    pending_signup['username'],
                                    pending_signup['password'],
                                    pending_signup['location'],
                                    pending_signup['birthdate'],
                                    pending_signup['profile'],
                                    phone_number,
                                )
                            )
                            db.execute(
                                'DELETE FROM pending_signups WHERE username = ?',
                                (pending_signup_username,)
                            )
                    except sqlite3.IntegrityError:
                        existing_user = query_db(
                            'SELECT id FROM users WHERE username = ?',
                            (pending_signup_username,),
                            one=True
                        )
                        session.pop('phone_verification', None)
                        if existing_user:
                            with db:
                                db.execute(
                                    'DELETE FROM pending_signups WHERE username = ?',
                                    (pending_signup_username,)
                                )
                            session.pop('pending_signup_username', None)
                            flash('That username is already in use. Please sign up again.', 'danger')
                            return redirect(url_for('signup'))
                        flash('That phone number is already linked to an account. Try another number.', 'danger')
                        return render_template(
                            'verify_phone.html.j2',
                            phone_number='',
                            demo_code=None,
                            is_signup=True,
                        )

                    new_user_id = cursor.lastrowid
                    username = pending_signup['username']
                    session.clear()
                    session['user_id'] = new_user_id
                    session['username'] = username
                    flash(f'Welcome, {username}! Your account has been created and your phone number verified.', 'success')
                    return redirect(url_for('feed'))

                db = get_db()
                with db:
                    db.execute(
                        'UPDATE users SET phone_number = ? WHERE id = ?',
                        (phone_number, user_id)
                    )
                session.pop('phone_verification', None)
                flash('Your phone number has been verified and linked.', 'success')
                return redirect(url_for('feed'))
        else:
            flash('Choose a phone number before entering a confirmation code.', 'warning')

    verification = session.get('phone_verification')
    return render_template(
        'verify_phone.html.j2',
        phone_number=verification['phone_number'] if verification else '',
        demo_code=verification['code'] if verification else None,
        is_signup=bool(pending_signup),
    )


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session.clear()
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    phone_redirect = require_verified_phone(user_id)
    if phone_redirect:
        return phone_redirect

    # Get content from the submitted form
    content = request.form.get('content')

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        db = get_db()
        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
        flash('Your comment was added.', 'success')
    else:
        flash('Comment cannot be empty.', 'warning')

    # Redirect back to the page the user came from (likely the post detail page)
    return redirect(request.referrer or url_for('post_detail', post_id=post_id))

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.coauthor_id, p.coauthor_content, p.created_at,
               u.id AS user_id, u.username, u.created_at as user_created_at,
               coauthor.username AS coauthor_username,
               NOT EXISTS (
                   SELECT 1 FROM posts previous
                   WHERE (previous.user_id = u.id OR previous.coauthor_id = u.id)
                     AND (previous.created_at < p.created_at
                          OR (previous.created_at = p.created_at AND previous.id < p.id))
               ) AS is_first_author_post,
               NOT EXISTS (
                   SELECT 1 FROM posts previous
                   WHERE (previous.user_id = coauthor.id OR previous.coauthor_id = coauthor.id)
                     AND (previous.created_at < p.created_at
                          OR (previous.created_at = p.created_at AND previous.id < p.id))
               ) AS is_first_coauthor_post
        FROM posts p
        JOIN users u ON p.user_id = u.id
        LEFT JOIN users coauthor ON p.coauthor_id = coauthor.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'



# ----- Functions to be implemented are below

def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts.
    Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.
    
    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113
    
    Materials: 
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook
    """

    followed_ids = set()
    positive_reaction_bonus = {}
    positive_reactions = ('like', 'love', 'wow', 'haha')

    if user_id:
        followed_users = query_db(
            'SELECT followed_id FROM follows WHERE follower_id = ?',
            (user_id,)
        )
        followed_ids = {row['followed_id'] for row in followed_users}

        positively_reacted_posts = query_db('''
            SELECT DISTINCT p.user_id, p.coauthor_id
            FROM reactions r
            JOIN posts p ON p.id = r.post_id
            WHERE r.user_id = ?
              AND r.reaction_type IN (?, ?, ?, ?)
        ''', (user_id, *positive_reactions))
        for post in positively_reacted_posts:
            for author_id in {post['user_id'], post['coauthor_id']} - {None, user_id}:
                positive_reaction_bonus[author_id] = positive_reaction_bonus.get(author_id, 0) + 1

    candidates = query_db('''
        SELECT p.id, p.content, p.coauthor_id, p.coauthor_content, p.created_at,
               u.username, u.id AS user_id, u.created_at AS user_created_at,
               coauthor.username AS coauthor_username,
               coauthor.created_at AS coauthor_created_at,
               NOT EXISTS (
                   SELECT 1 FROM posts previous
                   WHERE (previous.user_id = u.id OR previous.coauthor_id = u.id)
                     AND (previous.created_at < p.created_at
                          OR (previous.created_at = p.created_at AND previous.id < p.id))
               ) AS is_first_author_post,
               NOT EXISTS (
                   SELECT 1 FROM posts previous
                   WHERE (previous.user_id = coauthor.id OR previous.coauthor_id = coauthor.id)
                     AND (previous.created_at < p.created_at
                          OR (previous.created_at = p.created_at AND previous.id < p.id))
               ) AS is_first_coauthor_post
        FROM posts p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN users coauthor ON coauthor.id = p.coauthor_id
        WHERE (? IS NULL OR (p.user_id != ? AND (p.coauthor_id IS NULL OR p.coauthor_id != ?)))
          AND (? IS NULL OR NOT EXISTS (
              SELECT 1 FROM reactions r
              WHERE r.user_id = ? AND r.post_id = p.id
          ))
        ORDER BY p.created_at DESC, p.id DESC
    ''', (user_id, user_id, user_id, user_id, user_id))

    ranked_posts = []
    for post in candidates:
        author_ids = {post['user_id'], post['coauthor_id']} - {None, user_id}
        if filter_following and not author_ids.intersection(followed_ids):
            continue

        followed_author = bool(author_ids.intersection(followed_ids))
        interaction_score = max(
            (positive_reaction_bonus.get(author_id, 0) for author_id in author_ids),
            default=0
        )
        relevance_score = (3 if followed_author else 0) + interaction_score
        ranked_posts.append((relevance_score, post))

    ranked_posts.sort(key=lambda item: item[0], reverse=True)
    return [post for _, post in ranked_posts[:5]]

def user_risk_analysis(user_id):
    """
    Args:
        user_id: The ID of the user on which we perform risk analysis.

    Returns:
        A float number score showing the risk associated with this user. There are no strict rules or bounds to this score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk. (An upper bound of 5.0 is applied to this score elsewhere in the codebase) 
        
        You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
        Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    user = query_db(
        'SELECT profile, created_at FROM users WHERE id = ?',
        (user_id,),
        one=True
    )
    profile = user['profile'] or ''
    _, profile_score = moderate_content(profile)

    posts = query_db('''
        SELECT content
        FROM posts
        WHERE user_id = ?
        UNION ALL
        SELECT coauthor_content AS content
        FROM posts
        WHERE coauthor_id = ? AND coauthor_content IS NOT NULL
    ''', (user_id, user_id))
    post_scores = [moderate_content(post['content'])[1] for post in posts]
    average_post_score = sum(post_scores) / len(post_scores) if post_scores else 0.0

    comments = query_db(
        'SELECT content FROM comments WHERE user_id = ?',
        (user_id,)
    )
    comment_scores = [moderate_content(comment['content'])[1] for comment in comments]
    average_comment_score = (
        sum(comment_scores) / len(comment_scores) if comment_scores else 0.0
    )

    content_risk_score = profile_score + (average_post_score * 3) + average_comment_score
    account_age = datetime.now() - user['created_at']
    if account_age.days < 7:
        content_risk_score *= 1.5
    elif account_age.days < 30:
        content_risk_score *= 1.2

    if content_risk_score > 5.0:
        content_risk_score = 5.0

    return content_risk_score

    
def remove_url(match):
    url = match.group(0)
    trailing_punctuation = url[len(url.rstrip('.,!?;:)')):]
    return '[link removed]' + trailing_punctuation


def moderate_content(content):
    """
    Args
        content: the text content of a post or comment to be moderated.
        
    Returns: 
        A tuple containing the moderated content (string) and a severity score (float). There are no strict rules or bounds to the severity score, 
        other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk.
    
    This function moderates a string of content and calculates a severity score based on
    rules loaded from the 'censorship.dat' file. These are already loaded as TIER1_WORDS, TIER2_PHRASES and TIER3_WORDS. Tier 1 corresponds to strong profanity, Tier 2 to scam/spam phrases and Tier 3 to mild profanity.
    
    You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
    Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    score = 0.0

    tier1_pattern = re.compile(
        r'\b(?:' + '|'.join(re.escape(word) for word in TIER1_WORDS) + r')\b',
        re.IGNORECASE
    )
    if tier1_pattern.search(content):
        return "[content removed due to severe violation]", 5.0

    tier2_pattern = re.compile(
        r'(?<!\w)(?:' + '|'.join(
            re.escape(phrase)
            for phrase in sorted(TIER2_PHRASES, key=len, reverse=True)
        ) + r')(?!\w)',
        re.IGNORECASE
    )
    if tier2_pattern.search(content):
        return "[content removed due to spam/scam policy]", 5.0

    tier3_pattern = re.compile(
        r'\b(?:' + '|'.join(
            re.escape(word) for word in sorted(TIER3_WORDS, key=len, reverse=True)
        ) + r')\b',
        re.IGNORECASE
    )
    tier3_matches = list(tier3_pattern.finditer(content))
    score += len(tier3_matches) * 2.0
    moderated_content = tier3_pattern.sub(
        lambda match: '*' * len(match.group(0)),
        content
    )

    url_pattern = re.compile(
        r'\b(?:https?://|www\.)[^\s<>"\']+'
        r'|\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}'
        r'(?:[/?#][^\s<>"\']*)?',
        re.IGNORECASE
    )
    url_matches = list(url_pattern.finditer(moderated_content))
    score += len(url_matches) * 2.0

    for url_match in url_matches:
        moderated_content = moderated_content.replace(
            url_match.group(0),
            remove_url(url_match),
            1
        )

    letters = [character for character in content if character.isalpha()]
    if len(letters) > 15:
        uppercase_ratio = sum(character.isupper() for character in letters) / len(letters)
        if uppercase_ratio > 0.7:
            score += 0.5

    return moderated_content, score

if __name__ == '__main__':
    app.run(debug=True, port=8080)
