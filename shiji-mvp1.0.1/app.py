"""
校园食荐 - 校园餐饮推荐应用 MVP
后端主应用

技术栈: Flask + SQLAlchemy + SQLite + 规则+LLM双轨信息提取
"""
import os
import uuid
import random
import time
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

from utils import extract_info

app = Flask(__name__)
CORS(app)

# 配置
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///campus_food.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB 限制
app.config['UPLOAD_FOLDER'] = 'uploads'

# 确保上传目录存在
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)


# ========== 数据库模型 ==========

class Shop(db.Model):
    """店铺/窗口表"""
    id = db.Column(db.Integer, primary_key=True)
    canteen = db.Column(db.String(100), nullable=False)  # 食堂名称
    name = db.Column(db.String(100), nullable=False)      # 窗口名
    location_desc = db.Column(db.String(200))             # 位置描述
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('canteen', 'name', name='unique_canteen_shop'),
    )


class Post(db.Model):
    """帖子表"""
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(100), nullable=False)  # 设备标识
    text = db.Column(db.Text, nullable=False)              # 原始帖子内容
    images = db.Column(db.Text)                            # 图片路径，多个逗号分隔
    shop_id = db.Column(db.Integer, db.ForeignKey('shop.id'), nullable=True)
    dish_name = db.Column(db.String(100))                 # 提取的菜品名
    quote = db.Column(db.String(200))                     # 提取的评价摘录
    tags = db.Column(db.String(200))                      # 提取的标签，逗号分隔
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    shop = db.relationship('Shop', backref=db.backref('posts', lazy=True))


class Action(db.Model):
    """用户行为表（记录"我想吃"等）"""
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(100), nullable=False)
    target_type = db.Column(db.String(50), nullable=False)  # post / shop
    target_id = db.Column(db.Integer, nullable=False)
    action_type = db.Column(db.String(50), nullable=False)  # i_want_eat
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 简化唯一约束：同一用户同一天对同一个目标只能点一次
    # 应用层检查日期，数据库只约束组合
    __table_args__ = (
        db.UniqueConstraint(
            'device_id', 'target_type', 'target_id', 'action_type',
            name='unique_daily_action'
        ),
    )


# ========== 工具函数 ==========

def allowed_file(filename):
    """检查文件类型是否允许"""
    ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_or_create_shop(canteen, shop_name):
    """查找或创建店铺"""
    if not canteen or not shop_name:
        return None

    shop = Shop.query.filter_by(canteen=canteen, name=shop_name).first()
    if shop:
        return shop

    try:
        shop = Shop(canteen=canteen, name=shop_name)
        db.session.add(shop)
        db.session.commit()
        return shop
    except Exception as e:
        db.session.rollback()
        # 唯一约束冲突，说明已经被创建了，重新查询
        return Shop.query.filter_by(canteen=canteen, name=shop_name).first()


# ========== API 路由 ==========

@app.route('/')
def index():
    """首页，返回前端测试页面"""
    return app.send_static_file('index.html')


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """获取上传的图片"""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/api/recommendations', methods=['GET'])
def get_recommendations():
    """
    获取推荐卡片
    推荐策略：
    1. 最近48小时内被发帖提及的窗口/菜品 - 60%
    2. 48小时前但累计提及次数≥3的口碑窗口 - 30%
    3. 随机抽取剩余内容（保底） - 10%
    """
    try:
        now = datetime.utcnow()
        cutoff_48h = now - timedelta(hours=48)

        # 分类查询
        # 1. 最近48小时
        recent_posts = Post.query.filter(
            Post.created_at >= cutoff_48h,
            Post.shop_id.isnot(None),
            Post.dish_name.isnot(None)
        ).order_by(Post.created_at.desc()).all()

        # 2. 口碑内容（累计提及>=3，超过48小时）
        subquery = db.session.query(
            Post.shop_id,
            db.func.count(Post.id).label('count')
        ).filter(
            Post.created_at < cutoff_48h,
            Post.shop_id.isnot(None)
        ).group_by(Post.shop_id).having(db.func.count(Post.id) >= 3).subquery()

        popular_posts = Post.query\
            .join(subquery, Post.shop_id == subquery.c.shop_id)\
            .filter(Post.shop_id.isnot(None), Post.dish_name.isnot(None))\
            .order_by(Post.created_at.desc()).all()

        # 3. 剩余所有有效帖子
        all_valid_posts = Post.query.filter(
            Post.shop_id.isnot(None),
            Post.dish_name.isnot(None)
        ).order_by(Post.created_at.desc()).all()

        # 构建候选池
        candidates = []
        candidates.extend([(p, 3) for p in recent_posts])  # 最高权重

        for p in popular_posts:
            if p not in recent_posts:
                candidates.append((p, 2))  # 中等权重

        for p in all_valid_posts:
            if not any(p == cp for cp, _ in candidates):
                candidates.append((p, 1))  # 最低权重

        # 加权抽样
        if not candidates:
            return jsonify({
                "code": 0,
                "message": "暂无推荐数据",
                "data": []
            })

        # 根据权重选择
        weights = []
        for _, weight in candidates:
            if weight == 3:
                weights.append(60)  # 60%
            elif weight == 2:
                weights.append(30)  # 30%
            else:
                weights.append(10)  # 10%

        # 抽样最多10条
        num_to_pick = min(10, len(candidates))
        selected = random.choices(
            population=list(range(len(candidates))),
            weights=weights,
            k=num_to_pick
        )

        selected_posts = [candidates[i][0] for i in selected]
        # 去重
        seen = set()
        result_posts = []
        for p in selected_posts:
            if p.id not in seen:
                seen.add(p.id)
                result_posts.append(p)

        # 格式化输出
        result = []
        for post in result_posts:
            shop = post.shop
            if not shop:
                continue

            tags = post.tags.split(',') if post.tags else []
            tags = [t.strip() for t in tags if t.strip()]

            image_url = None
            if post.images:
                first_image = post.images.split(',')[0].strip()
                if first_image:
                    image_url = f"/uploads/{first_image}"

            result.append({
                "id": post.id,
                "shop_id": shop.id,
                "canteen": shop.canteen,
                "shop_name": shop.name,
                "dish_name": post.dish_name,
                "quote": post.quote,
                "tags": tags,
                "image_url": image_url,
                "created_at": post.created_at.isoformat() if post.created_at else None
            })

        # 如果推荐池为空，提示用户发帖
        has_more = len(all_valid_posts) >= len(result)
        if not has_more and len(result) > 0:
            # 已展示所有，返回提示信息
            pass

        return jsonify({
            "code": 0,
            "message": "success",
            "data": result,
            "meta": {
                "total": len(all_valid_posts),
                "returned": len(result),
                "has_more": has_more
            }
        })

    except Exception as e:
        print(f"获取推荐失败: {e}")
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        }), 500


@app.route('/api/posts', methods=['POST'])
def create_post():
    """
    创建新帖子
    接收: multipipart/form-data
    - text: 文字内容
    - images: 图片文件（多个）
    - device_id: 设备标识
    """
    try:
        device_id = request.form.get('device_id')
        text = request.form.get('text')

        if not device_id:
            return jsonify({"code": 400, "message": "缺少device_id", "data": None}), 400
        if not text or len(text.strip()) < 3:
            return jsonify({"code": 400, "message": "内容太短", "data": None}), 400

        # 处理图片上传
        image_paths = []
        if 'images' in request.files:
            files = request.files.getlist('images')
            for file in files:
                if file and file.filename and allowed_file(file.filename):
                    ext = file.filename.rsplit('.', 1)[1].lower()
                    filename = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.{ext}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    image_paths.append(filename)

        images_str = ','.join(image_paths) if image_paths else None

        # 使用AI提取结构化信息
        info = extract_info(text)

        canteen = info.get('canteen')
        shop_name = info.get('shop_name')
        dish_name = info.get('dish_name')
        quote = info.get('quote')
        tags = info.get('tags', [])

        # 查找或创建店铺
        shop = None
        if canteen and shop_name:
            shop = get_or_create_shop(canteen, shop_name)

        # 创建帖子
        post = Post(
            device_id=device_id,
            text=text,
            images=images_str,
            shop_id=shop.id if shop else None,
            dish_name=dish_name,
            quote=quote,
            tags=','.join(tags) if tags else None
        )

        db.session.add(post)
        db.session.commit()

        return jsonify({
            "code": 0,
            "message": "success",
            "data": {
                "post_id": post.id,
                "extracted": info
            }
        })

    except Exception as e:
        db.session.rollback()
        print(f"创建帖子失败: {e}")
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        }), 500


@app.route('/api/shops/<int:shop_id>', methods=['GET'])
def get_shop_detail(shop_id):
    """获取店铺详情和该店铺下的所有帖子"""
    try:
        shop = Shop.query.get(shop_id)
        if not shop:
            return jsonify({
                "code": 404,
                "message": "店铺不存在",
                "data": None
            }), 404

        posts = Post.query\
            .filter_by(shop_id=shop_id)\
            .order_by(Post.created_at.desc())\
            .all()

        posts_data = []
        for post in posts:
            tags = post.tags.split(',') if post.tags else []
            tags = [t.strip() for t in tags if t.strip()]

            images = []
            if post.images:
                for img in post.images.split(','):
                    img = img.strip()
                    if img:
                        images.append(f"/uploads/{img}")

            posts_data.append({
                "id": post.id,
                "text": post.text,
                "dish_name": post.dish_name,
                "quote": post.quote,
                "tags": tags,
                "images": images,
                "created_at": post.created_at.isoformat() if post.created_at else None
            })

        return jsonify({
            "code": 0,
            "message": "success",
            "data": {
                "shop": {
                    "id": shop.id,
                    "canteen": shop.canteen,
                    "name": shop.name,
                    "location_desc": shop.location_desc
                },
                "posts": posts_data,
                "total": len(posts_data)
            }
        })

    except Exception as e:
        print(f"获取店铺详情失败: {e}")
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        }), 500


@app.route('/api/actions', methods=['POST'])
def record_action():
    """记录用户行为（如"我想吃"）"""
    try:
        data = request.get_json()
        device_id = data.get('device_id')
        target_type = data.get('target_type')
        target_id = data.get('target_id')
        action_type = data.get('action_type', 'i_want_eat')

        if not all([device_id, target_type, target_id]):
            return jsonify({
                "code": 400,
                "message": "缺少必要参数",
                "data": None
            }), 400

        action = Action(
            device_id=device_id,
            target_type=target_type,
            target_id=target_id,
            action_type=action_type
        )
        db.session.add(action)
        db.session.commit()

        return jsonify({
            "code": 0,
            "message": "success",
            "data": {"action_id": action.id}
        })

    except db.exc.IntegrityError:
        db.session.rollback()
        # 唯一约束冲突，说明今天已经点过了，也算成功
        return jsonify({
            "code": 0,
            "message": "already recorded today",
            "data": None
        })
    except Exception as e:
        db.session.rollback()
        print(f"记录行为失败: {e}")
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        }), 500


# ========== 错误处理 ==========

@app.errorhandler(413)
def too_large(e):
    return jsonify({
        "code": 413,
        "message": "文件太大，最大支持5MB",
        "data": None
    }), 413


# ========== 初始化数据库 ==========

def init_db():
    """初始化数据库并创建表"""
    with app.app_context():
        db.create_all()
        print("数据库初始化完成")


if __name__ == '__main__':
    # 初始化数据库
    init_db()

    # 启动应用
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('DEBUG', 'True').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
