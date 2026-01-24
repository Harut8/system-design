# Permission code constants for type-safe permission checks

# User permissions
USERS_READ = "users:read"
USERS_WRITE = "users:write"
USERS_DELETE = "users:delete"

# Role permissions
ROLES_READ = "roles:read"
ROLES_WRITE = "roles:write"

# Post permissions
POSTS_READ = "posts:read"
POSTS_WRITE = "posts:write"
POSTS_DELETE = "posts:delete"

# Admin permissions
ADMIN_ACCESS = "admin:access"

# Permission groups
USER_MANAGEMENT_PERMISSIONS = [USERS_READ, USERS_WRITE, USERS_DELETE]
ROLE_MANAGEMENT_PERMISSIONS = [ROLES_READ, ROLES_WRITE]
CONTENT_PERMISSIONS = [POSTS_READ, POSTS_WRITE, POSTS_DELETE]

# Role codes
SUPER_ADMIN = "super_admin"
ADMIN = "admin"
MANAGER = "manager"
USER = "user"
VIEWER = "viewer"
