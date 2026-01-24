-- FastAPI RBAC Seed Data Migration
-- Version: 002
-- Description: Seeds default permissions and roles

-- Insert default permissions
INSERT INTO permissions (code, name, resource, action, is_system) VALUES
    ('users:read', 'View Users', 'users', 'read', TRUE),
    ('users:write', 'Create/Update Users', 'users', 'write', TRUE),
    ('users:delete', 'Delete Users', 'users', 'delete', TRUE),
    ('roles:read', 'View Roles', 'roles', 'read', TRUE),
    ('roles:write', 'Manage Roles', 'roles', 'write', TRUE),
    ('posts:read', 'View Posts', 'posts', 'read', TRUE),
    ('posts:write', 'Create/Update Posts', 'posts', 'write', TRUE),
    ('posts:delete', 'Delete Posts', 'posts', 'delete', TRUE),
    ('admin:access', 'Admin Panel Access', 'admin', 'access', TRUE)
ON CONFLICT (code) DO NOTHING;

-- Insert default global roles (tenant_id = NULL for global)
INSERT INTO roles (code, name, description, is_system, priority, tenant_id) VALUES
    ('super_admin', 'Super Administrator', 'Full system access', TRUE, 100, NULL),
    ('admin', 'Administrator', 'Tenant administrator', TRUE, 80, NULL),
    ('manager', 'Manager', 'Department manager', TRUE, 60, NULL),
    ('user', 'User', 'Standard user', TRUE, 40, NULL),
    ('viewer', 'Viewer', 'Read-only access', TRUE, 20, NULL)
ON CONFLICT (tenant_id, code) DO NOTHING;

-- Assign permissions to super_admin role (all permissions)
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.code = 'super_admin'
ON CONFLICT DO NOTHING;

-- Assign permissions to admin role
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.code = 'admin'
  AND p.code IN ('users:read', 'users:write', 'users:delete', 'roles:read', 'roles:write', 'admin:access', 'posts:read', 'posts:write', 'posts:delete')
ON CONFLICT DO NOTHING;

-- Assign permissions to manager role
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.code = 'manager'
  AND p.code IN ('users:read', 'posts:read', 'posts:write', 'posts:delete')
ON CONFLICT DO NOTHING;

-- Assign permissions to user role
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.code = 'user'
  AND p.code IN ('posts:read', 'posts:write')
ON CONFLICT DO NOTHING;

-- Assign permissions to viewer role
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.code = 'viewer'
  AND p.code IN ('posts:read')
ON CONFLICT DO NOTHING;

-- Set up role hierarchy
-- admin inherits from manager
INSERT INTO role_hierarchy (parent_role_id, child_role_id)
SELECT
    (SELECT id FROM roles WHERE code = 'manager'),
    (SELECT id FROM roles WHERE code = 'admin')
ON CONFLICT DO NOTHING;

-- manager inherits from user
INSERT INTO role_hierarchy (parent_role_id, child_role_id)
SELECT
    (SELECT id FROM roles WHERE code = 'user'),
    (SELECT id FROM roles WHERE code = 'manager')
ON CONFLICT DO NOTHING;

-- user inherits from viewer
INSERT INTO role_hierarchy (parent_role_id, child_role_id)
SELECT
    (SELECT id FROM roles WHERE code = 'viewer'),
    (SELECT id FROM roles WHERE code = 'user')
ON CONFLICT DO NOTHING;
