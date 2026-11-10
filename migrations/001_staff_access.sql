-- Run once on an existing database that does not yet have staff access.
WHENEVER SQLERROR EXIT SQL.SQLCODE ROLLBACK
ALTER SESSION SET CONTAINER = FREEPDB1;
ALTER SESSION SET CURRENT_SCHEMA = SYSTEM;

ALTER TABLE Users ADD (
    role VARCHAR2(20) DEFAULT 'customer' NOT NULL,
    restaurant_id NUMBER,
    CONSTRAINT chk_user_role CHECK (role IN ('customer', 'staff')),
    CONSTRAINT chk_staff_restaurant CHECK (role <> 'staff' OR restaurant_id IS NOT NULL),
    CONSTRAINT fk_user_restaurant FOREIGN KEY (restaurant_id) REFERENCES Restaurants(restaurant_id)
);

INSERT INTO Users (email, password_hash, first_name, role, restaurant_id)
SELECT CASE restaurant_id
           WHEN 1 THEN 'burger.staff@example.com'
           WHEN 2 THEN 'pizza.staff@example.com'
           WHEN 3 THEN 'taco.staff@example.com'
       END,
       'scrypt:32768:8:1$z3HavGn8ET4npZpJ$14775cfe02187006c0ca37d61cf3bd247c14b14a30380fabd7d72bd87e9a54c2555f34edd3770d23052312e12d7fd32f645d0a259d30139f084c7766d41592b5',
       name || ' Staff', 'staff', restaurant_id
FROM Restaurants
WHERE restaurant_id IN (1, 2, 3);

COMMIT;
EXIT
