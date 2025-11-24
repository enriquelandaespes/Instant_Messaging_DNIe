#!/usr/bin/env python3
"""Script para limpiar duplicados de la base de datos"""

from database import JsonDatabase

if __name__ == "__main__":
    print("Limpiando duplicados de la base de datos...")
    db = JsonDatabase()
    
    print(f"\nContactos después de limpiar:")
    for cn, info in db.get_all_contacts().items():
        print(f"  - {cn}: {info.get('name')} ({info.get('ip')}:{info.get('port')})")
    
    print("\n✓ Base de datos limpia")
