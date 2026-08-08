from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False)

class Workbook(Base):
    __tablename__ = 'workbooks'

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, nullable=False)
    modified_at = Column(DateTime, nullable=False)
    settings = Column(JSON)

    owner = relationship("User", back_populates="workbooks")
    worksheets = relationship("Worksheet", back_populates="workbook", cascade="all, delete-orphan")

class Worksheet(Base):
    __tablename__ = 'worksheets'

    id = Column(Integer, primary_key=True)
    workbook_id = Column(Integer, ForeignKey('workbooks.id'), nullable=False)
    name = Column(String, nullable=False)
    order = Column(Integer, nullable=False)

    workbook = relationship("Workbook", back_populates="worksheets")
    cells = relationship("Cell", back_populates="worksheet", cascade="all, delete-orphan")

# HUMAN ASSISTANCE NEEDED
# The Cell model has a confidence level of 0.7, which is below the threshold of 0.8.
# Please review and adjust the following implementation as needed.
class Cell(Base):
    __tablename__ = 'cells'

    id = Column(Integer, primary_key=True)
    worksheet_id = Column(Integer, ForeignKey('worksheets.id'), nullable=False)
    row = Column(Integer, nullable=False)
    column = Column(Integer, nullable=False)
    value = Column(String)
    formula = Column(String)
    style = Column(JSON)

    worksheet = relationship("Worksheet", back_populates="cells")